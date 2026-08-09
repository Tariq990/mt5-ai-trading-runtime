from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Settings
from .models import AccountState, Decision, DecisionAction, MarketPacket, Quote, SymbolContractSpec


@dataclass(frozen=True)
class SafetyResult:
    allowed: bool
    code: str
    reason: str


# Material change threshold: if broker mechanics (volume min/step) would move the
# order size more than this far from ChatGPT's requested size, the decision is
# rejected instead of silently trading a different economic intent.
NORMALIZATION_DRIFT_MAX_PCT = 25.0


class SafetyEngine:
    def __init__(self, settings: Settings):
        self.settings = settings

    def account_gate(self, account: AccountState) -> SafetyResult:
        if not account.is_demo:
            return SafetyResult(False, "REAL_ACCOUNT_BLOCKED", "Only demo accounts are allowed.")
        daily_start = account.daily_start_equity
        if daily_start is None:
            daily_start = max(account.equity - account.daily_pnl, 1e-9)
        if account.daily_pnl < 0:
            loss_pct = abs(account.daily_pnl) / max(daily_start, 1e-9) * 100
            if loss_pct >= self.settings.daily_loss_limit_pct:
                return SafetyResult(False, "DAILY_LOSS_LOCK", "Daily loss limit reached.")
        if account.trailing_drawdown_pct >= self.settings.trailing_dd_limit_pct:
            return SafetyResult(False, "TRAILING_DD_LOCK", "Trailing drawdown limit reached.")
        return SafetyResult(True, "OK", "Account safety checks passed.")

    def packet_gate(self, packet: MarketPacket) -> SafetyResult:
        age = (datetime.now(timezone.utc) - packet.broker_timestamp.astimezone(timezone.utc)).total_seconds()
        if age > self.settings.packet_max_age_seconds:
            return SafetyResult(False, "STALE_PACKET", f"Packet is {age:.1f}s old.")
        if age < -self.settings.quote_future_skew_tolerance_seconds:
            return SafetyResult(False, "STALE_PACKET", f"Packet timestamps are {age:.1f}s in the future (broker clock/offset misconfigured).")
        if not packet.account.is_demo:
            return SafetyResult(False, "REAL_ACCOUNT_BLOCKED", "Only demo accounts are allowed.")
        # Daily/DD locks block NEW risk, not protective management/close actions.
        return SafetyResult(True, "OK", "Packet freshness/demo checks passed.")

    def decision_gate(self, packet: MarketPacket, decision: Decision) -> SafetyResult:
        if decision.cycle_id != packet.cycle_id:
            return SafetyResult(False, "CYCLE_MISMATCH", "Decision cycle_id does not match packet.")
        now = datetime.now(timezone.utc)
        if decision.valid_until.astimezone(timezone.utc) <= now:
            return SafetyResult(False, "DECISION_EXPIRED", "Decision validity window expired.")
        if decision.decision == DecisionAction.WAIT:
            return SafetyResult(True, "WAIT", "No execution required.")
        if decision.symbol not in packet.symbols:
            return SafetyResult(False, "UNKNOWN_SYMBOL", "Decision symbol is absent from packet.")
        symbol_data = packet.symbols[decision.symbol]
        quote_age = (datetime.now(timezone.utc) - symbol_data.quote.ts.astimezone(timezone.utc)).total_seconds()
        if quote_age > self.settings.packet_max_age_seconds:
            return SafetyResult(False, "STALE_SYMBOL_QUOTE", f"{decision.symbol} quote is {quote_age:.1f}s old.")
        if quote_age < -self.settings.quote_future_skew_tolerance_seconds:
            return SafetyResult(False, "STALE_SYMBOL_QUOTE", f"{decision.symbol} quote is {quote_age:.1f}s in the future (broker clock/offset misconfigured).")
        contract = symbol_data.contract
        trade_mode = (
            contract.trade_mode_label
            if contract is not None and contract.trade_mode_label
            else str(symbol_data.market.get("trade_mode_label", "FULL")).upper()
        )
        if decision.decision in {DecisionAction.LONG, DecisionAction.SHORT}:
            if trade_mode in {"DISABLED", "CLOSEONLY"}:
                return SafetyResult(False, "SYMBOL_NOT_OPEN_FOR_ENTRY", f"{decision.symbol} trade mode is {trade_mode}.")
            if decision.decision == DecisionAction.LONG and trade_mode == "SHORTONLY":
                return SafetyResult(False, "SYMBOL_SHORT_ONLY", f"{decision.symbol} currently permits short entries only.")
            if decision.decision == DecisionAction.SHORT and trade_mode == "LONGONLY":
                return SafetyResult(False, "SYMBOL_LONG_ONLY", f"{decision.symbol} currently permits long entries only.")
        if decision.decision in {DecisionAction.LONG, DecisionAction.SHORT}:
            # Fail-closed aggregate: a symbol may accept NEW entries only when
            # the broker quote is fresh, the market session is open and the
            # broker trade mode allows entries. Protective actions below stay
            # available even when the symbol is not executable.
            status = packet.symbol_status.get(decision.symbol)
            if status is not None and not status.executable_now:
                return SafetyResult(
                    False,
                    "SYMBOL_NOT_EXECUTABLE",
                    f"{decision.symbol} cannot accept new entries right now: {status.reason or status.status}",
                )
            account = self.account_gate(packet.account)
            if not account.allowed:
                return account
            # MT5 netting/exchange accounts do not preserve independent same-symbol
            # positions: a new order can merge, reduce, close, or reverse the existing
            # position. GPTTRADDER promises independent setups, so fail closed unless
            # the broker account explicitly supports hedging.
            if packet.account.hedging_allowed is False and any(p.symbol == decision.symbol for p in packet.positions):
                return SafetyResult(
                    False,
                    "NETTING_POSITION_CONFLICT",
                    f"{decision.symbol} already has a position but account mode {packet.account.account_mode or 'NETTING'} cannot preserve an independent new setup.",
                )
            if decision.risk_percent is not None and decision.risk_percent > self.settings.daily_loss_limit_pct:
                return SafetyResult(False, "RISK_TOO_HIGH", "Single trade risk exceeds daily loss ceiling.")
            geometry = self.trade_geometry_gate(decision, packet.symbols[decision.symbol].quote)
            if not geometry.allowed:
                return geometry
            contract_gate = self.contract_gate(decision, contract)
            if not contract_gate.allowed:
                return contract_gate
            volume_gate = self.volume_normalization_gate(decision, contract)
            if not volume_gate.allowed:
                return volume_gate
        elif decision.decision in {DecisionAction.MANAGE_POSITION, DecisionAction.CLOSE_POSITION}:
            position_id = decision.management.position_id if decision.management else decision.position_id
            if not any(p.position_id == position_id and p.symbol == decision.symbol for p in packet.positions):
                return SafetyResult(False, "UNKNOWN_POSITION", "Requested position is absent from broker packet.")
        elif decision.decision == DecisionAction.CANCEL_ORDER:
            if not any(o.order_id == decision.pending_order_id and o.symbol == decision.symbol for o in packet.pending_orders):
                return SafetyResult(False, "UNKNOWN_PENDING_ORDER", "Requested pending order is absent from broker packet.")
        return self.execution_price_gate(decision, packet.symbols[decision.symbol].quote)


    def contract_gate(self, decision: Decision, contract: SymbolContractSpec | None) -> SafetyResult:
        """Hard rule: no new trade without sufficient broker contract metadata."""
        if decision.decision not in {DecisionAction.LONG, DecisionAction.SHORT}:
            return SafetyResult(True, "OK", "No contract checks required.")
        if contract is None:
            return SafetyResult(
                False,
                "CONTRACT_METADATA_MISSING",
                f"Broker contract metadata for {decision.symbol} is missing; ChatGPT cannot verify sizing/risk.",
            )
        if not contract.is_tradable:
            return SafetyResult(
                False,
                "SYMBOL_NOT_OPEN_FOR_ENTRY",
                f"{decision.symbol} is not open for new entries (trade mode {contract.trade_mode_label or 'unknown'}).",
            )
        missing = [field for field in ("volume_min", "volume_step", "trade_contract_size", "trade_tick_value") if getattr(contract, field) is None]
        if missing:
            return SafetyResult(
                False,
                "CONTRACT_METADATA_MISSING",
                f"{decision.symbol} contract lacks {', '.join(missing)}; sizing cannot be verified.",
            )
        return SafetyResult(True, "OK", "Contract metadata is sufficient.")

    def volume_normalization_gate(self, decision: Decision, contract: SymbolContractSpec | None) -> SafetyResult:
        """Reject when broker volume normalization would change ChatGPT's intent."""
        if decision.decision not in {DecisionAction.LONG, DecisionAction.SHORT} or not decision.order:
            return SafetyResult(True, "OK", "No volume normalization required.")
        if contract is None or contract.volume_min is None or contract.volume_step is None:
            return SafetyResult(
                False,
                "CONTRACT_METADATA_MISSING",
                f"{decision.symbol} volume constraints are unavailable; cannot normalize the requested size.",
            )
        from .risk import normalize_volume

        volume_max = contract.volume_max if contract.volume_max is not None else contract.volume_min * 1_000_000.0
        normalized = normalize_volume(decision.order.size, contract.volume_min, volume_max, contract.volume_step)
        if normalized <= 0:
            return SafetyResult(
                False,
                "VOLUME_BELOW_MINIMUM",
                f"Requested size {decision.order.size} is below broker minimum {contract.volume_min} for {decision.symbol}.",
            )
        drift_pct = abs(normalized - decision.order.size) / decision.order.size * 100.0
        if drift_pct > NORMALIZATION_DRIFT_MAX_PCT:
            return SafetyResult(
                False,
                "NORMALIZATION_DRIFT",
                f"Broker normalization moves {decision.symbol} size from {decision.order.size} to {normalized} ({drift_pct:.1f}% drift) which materially changes ChatGPT's risk intent.",
            )
        return SafetyResult(True, "OK", f"Volume normalizes to {normalized} within tolerance.")

    def trade_geometry_gate(self, decision: Decision, quote: Quote) -> SafetyResult:
        if decision.decision not in {DecisionAction.LONG, DecisionAction.SHORT} or not decision.order:
            return SafetyResult(True, "OK", "No trade geometry checks required.")
        quote_ok = self.quote_sanity_check(quote)
        if not quote_ok.allowed:
            return quote_ok
        from .models import OrderType

        is_long = decision.decision == DecisionAction.LONG
        market = quote.ask if is_long else quote.bid
        entry = decision.order.entry if decision.order.entry is not None else market
        assert decision.stop_loss is not None
        if is_long and decision.stop_loss >= entry:
            return SafetyResult(False, "INVALID_STOP_GEOMETRY", "LONG stop_loss must be below entry.")
        if not is_long and decision.stop_loss <= entry:
            return SafetyResult(False, "INVALID_STOP_GEOMETRY", "SHORT stop_loss must be above entry.")
        for target in decision.take_profit:
            if is_long and target.price <= entry:
                return SafetyResult(False, "INVALID_TP_GEOMETRY", "LONG take-profit must be above entry.")
            if not is_long and target.price >= entry:
                return SafetyResult(False, "INVALID_TP_GEOMETRY", "SHORT take-profit must be below entry.")
        if decision.order.type == OrderType.LIMIT:
            if is_long and entry >= quote.ask:
                return SafetyResult(False, "INVALID_PENDING_GEOMETRY", "BUY LIMIT entry must be below current ask.")
            if not is_long and entry <= quote.bid:
                return SafetyResult(False, "INVALID_PENDING_GEOMETRY", "SELL LIMIT entry must be above current bid.")
        elif decision.order.type == OrderType.STOP:
            if is_long and entry <= quote.ask:
                return SafetyResult(False, "INVALID_PENDING_GEOMETRY", "BUY STOP entry must be above current ask.")
            if not is_long and entry >= quote.bid:
                return SafetyResult(False, "INVALID_PENDING_GEOMETRY", "SELL STOP entry must be below current bid.")
        return SafetyResult(True, "OK", "Trade geometry checks passed.")

    def quote_sanity_check(self, quote: Quote) -> SafetyResult:
        if quote.ask <= 0 or quote.bid <= 0:
            return SafetyResult(False, "INVALID_QUOTE", f"Market is closed or no tick: bid={quote.bid}, ask={quote.ask}.")
        if quote.ask < quote.bid:
            return SafetyResult(False, "INVALID_QUOTE", f"Broker quote inverted: bid={quote.bid}, ask={quote.ask}.")
        if quote.spread < 0:
            return SafetyResult(False, "INVALID_QUOTE", f"Broker quote has a negative spread: {quote.spread}.")
        return SafetyResult(True, "OK", "Quote is sane.")

    def execution_price_gate(self, decision: Decision, quote: Quote) -> SafetyResult:
        quote_ok = self.quote_sanity_check(quote)
        if not quote_ok.allowed:
            return quote_ok
        now = datetime.now(timezone.utc)
        if decision.valid_until.astimezone(timezone.utc) <= now:
            return SafetyResult(False, "DECISION_EXPIRED", "Decision validity window expired before execution.")
        quote_age = (now - quote.ts.astimezone(timezone.utc)).total_seconds()
        if quote_age > self.settings.packet_max_age_seconds:
            return SafetyResult(False, "STALE_EXECUTION_QUOTE", f"Fresh broker quote is actually {quote_age:.1f}s old.")
        if not decision.order or not decision.order.acceptable_price_range:
            return SafetyResult(True, "OK", "Execution price checks passed.")
        from .models import OrderType
        lo, hi = decision.order.acceptable_price_range
        if decision.order.type != OrderType.MARKET:
            if decision.order.entry is None or not lo <= decision.order.entry <= hi:
                return SafetyResult(False, "ENTRY_OUTSIDE_RANGE", "Pending entry is outside allowed range.")
            return SafetyResult(True, "OK", "Pending entry price checks passed.")
        if decision.decision == DecisionAction.LONG:
            market_price = quote.ask
        elif decision.decision == DecisionAction.SHORT:
            market_price = quote.bid
        else:
            return SafetyResult(True, "OK", "No entry price range applies to this action.")
        if not lo <= market_price <= hi:
            return SafetyResult(
                False,
                "PRICE_OUTSIDE_RANGE",
                f"Fresh broker price {market_price} left allowed range [{lo}, {hi}].",
            )
        return SafetyResult(True, "OK", "Execution price checks passed.")

    def risk_budget_gate(
        self,
        account: AccountState,
        decision: Decision,
        decision_risk_amount: float | None,
        open_risk_amount: float | None,
    ) -> SafetyResult:
        if decision.decision not in {DecisionAction.LONG, DecisionAction.SHORT}:
            return SafetyResult(True, "OK", "No new position risk to validate.")
        if decision.risk_percent is None:
            return SafetyResult(False, "RISK_MISSING", "Entry decision is missing risk_percent.")
        if decision_risk_amount is None:
            return SafetyResult(False, "RISK_UNAVAILABLE", "Broker could not calculate stop-loss risk.")
        if open_risk_amount is None:
            return SafetyResult(False, "OPEN_RISK_UNAVAILABLE", "Broker could not calculate current open risk.")
        allowed_trade_risk = account.equity * decision.risk_percent / 100.0
        tolerance = max(0.01, allowed_trade_risk * 0.02)
        if decision_risk_amount > allowed_trade_risk + tolerance:
            return SafetyResult(
                False,
                "SIZE_EXCEEDS_RISK",
                f"Order stop risk {decision_risk_amount:.2f} exceeds {decision.risk_percent:.3f}% budget ({allowed_trade_risk:.2f}).",
            )
        daily_start = account.daily_start_equity or max(account.equity - account.daily_pnl, 1e-9)
        max_daily_loss = daily_start * self.settings.daily_loss_limit_pct / 100.0
        realized_or_floating_loss = max(0.0, -account.daily_pnl)
        remaining_daily_loss = max(0.0, max_daily_loss - realized_or_floating_loss)
        total_stop_risk = open_risk_amount + decision_risk_amount
        if total_stop_risk > remaining_daily_loss + tolerance:
            return SafetyResult(
                False,
                "TOTAL_RISK_EXCEEDS_DAILY_HEADROOM",
                "Existing stop risk plus new stop risk exceeds remaining daily loss headroom.",
            )
        trailing_floor = account.peak_equity * (1.0 - self.settings.trailing_dd_limit_pct / 100.0)
        remaining_trailing_headroom = max(0.0, account.equity - trailing_floor)
        if total_stop_risk > remaining_trailing_headroom + tolerance:
            return SafetyResult(
                False,
                "TOTAL_RISK_EXCEEDS_TRAILING_HEADROOM",
                "Existing stop risk plus new stop risk exceeds remaining trailing-drawdown headroom.",
            )
        return SafetyResult(True, "OK", "Risk budget checks passed.")
