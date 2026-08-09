from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Settings
from .models import AccountState, Decision, DecisionAction, MarketPacket


@dataclass(frozen=True)
class SafetyResult:
    allowed: bool
    code: str
    reason: str


class SafetyEngine:
    def __init__(self, settings: Settings):
        self.settings = settings

    def account_gate(self, account: AccountState) -> SafetyResult:
        if not account.is_demo:
            return SafetyResult(False, "REAL_ACCOUNT_BLOCKED", "Only demo accounts are allowed.")
        if account.daily_pnl < 0:
            loss_pct = abs(account.daily_pnl) / max(account.balance - account.daily_pnl, 1e-9) * 100
            if loss_pct >= self.settings.daily_loss_limit_pct:
                return SafetyResult(False, "DAILY_LOSS_LOCK", "Daily loss limit reached.")
        if account.trailing_drawdown_pct >= self.settings.trailing_dd_limit_pct:
            return SafetyResult(False, "TRAILING_DD_LOCK", "Trailing drawdown limit reached.")
        return SafetyResult(True, "OK", "Account safety checks passed.")

    def packet_gate(self, packet: MarketPacket) -> SafetyResult:
        age = (datetime.now(timezone.utc) - packet.broker_timestamp.astimezone(timezone.utc)).total_seconds()
        if age > self.settings.packet_max_age_seconds:
            return SafetyResult(False, "STALE_PACKET", f"Packet is {age:.1f}s old.")
        return self.account_gate(packet.account)

    def decision_gate(self, packet: MarketPacket, decision: Decision) -> SafetyResult:
        if decision.cycle_id != packet.cycle_id:
            return SafetyResult(False, "CYCLE_MISMATCH", "Decision cycle_id does not match packet.")
        now = datetime.now(timezone.utc)
        if decision.valid_until.astimezone(timezone.utc) <= now:
            return SafetyResult(False, "DECISION_EXPIRED", "Decision validity window expired.")
        if decision.decision == DecisionAction.WAIT:
            return SafetyResult(True, "WAIT", "No execution required.")
        account = self.account_gate(packet.account)
        if not account.allowed:
            return account
        if decision.symbol not in packet.symbols:
            return SafetyResult(False, "UNKNOWN_SYMBOL", "Decision symbol is absent from packet.")
        if decision.risk_percent is not None and decision.risk_percent > self.settings.daily_loss_limit_pct:
            return SafetyResult(False, "RISK_TOO_HIGH", "Single trade risk exceeds daily loss ceiling.")
        if decision.order and decision.order.acceptable_price_range:
            quote = packet.symbols[decision.symbol].quote
            market_price = quote.ask if decision.decision == DecisionAction.LONG else quote.bid
            lo, hi = decision.order.acceptable_price_range
            if not lo <= market_price <= hi:
                return SafetyResult(False, "PRICE_OUTSIDE_RANGE", "Current broker price left allowed range.")
        return SafetyResult(True, "OK", "Decision safety checks passed.")
