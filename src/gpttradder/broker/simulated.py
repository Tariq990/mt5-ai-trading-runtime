from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence
from uuid import uuid4

from .base import Broker
from ..models import (
    AccountState,
    Candle,
    Decision,
    DecisionAction,
    ExecutionResult,
    ManagementAction,
    OrderType,
    PendingOrder,
    Position,
    Quote,
    Side,
    SymbolContractSpec,
    SymbolMarketData,
)
from ..risk import normalize_volume
from ..symbols import SymbolResolution, resolve_canonical_symbols

# Broker-style instrument names: the simulated adapter pretends to be a real CFD
# broker so the canonical->broker resolution path stays fully exercised in tests.
SIM_BROKER_SYMBOLS = ("BTCUSD", "ETHUSD", "XAUUSD", "EURUSD", "GBPUSD")

# Per-broker-symbol simulation constants: volatility (per tick), spread (fraction
# of price), and realistic contract metadata per asset class.
SIM_PROFILE: dict[str, dict] = {
    "BTCUSD": {
        "description": "Bitcoin vs US Dollar",
        "volatility": 0.0012,
        "spread_bps": 8,
        "trade_contract_size": 1.0,
        "digits": 2,
        "point": 0.01,
        "trade_tick_size": 0.01,
        "trade_tick_value": 0.01,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "volume_limit": 0,
        "currency_base": "BTC",
        "currency_profit": "USD",
        "currency_margin": "USD",
        "swap_long": -0.02,
        "swap_short": -0.02,
    },
    "ETHUSD": {
        "description": "Ethereum vs US Dollar",
        "volatility": 0.0016,
        "spread_bps": 8,
        "trade_contract_size": 1.0,
        "digits": 2,
        "point": 0.01,
        "trade_tick_size": 0.01,
        "trade_tick_value": 0.01,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "volume_limit": 0,
        "currency_base": "ETH",
        "currency_profit": "USD",
        "currency_margin": "USD",
        "swap_long": -0.02,
        "swap_short": -0.02,
    },
    "XAUUSD": {
        "description": "Gold Spot vs US Dollar",
        "volatility": 0.0007,
        "spread_bps": 5,
        "trade_contract_size": 100.0,
        "digits": 2,
        "point": 0.01,
        "trade_tick_size": 0.01,
        "trade_tick_value": 1.0,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "volume_limit": 0,
        "currency_base": "XAU",
        "currency_profit": "USD",
        "currency_margin": "USD",
        "swap_long": -4.0,
        "swap_short": 1.0,
    },
    "EURUSD": {
        "description": "Euro vs US Dollar",
        "volatility": 0.00025,
        "spread_bps": 1.2,
        "trade_contract_size": 100_000.0,
        "digits": 5,
        "point": 0.00001,
        "trade_tick_size": 0.00001,
        "trade_tick_value": 1.0,
        "volume_min": 0.01,
        "volume_max": 200.0,
        "volume_step": 0.01,
        "volume_limit": 0,
        "currency_base": "EUR",
        "currency_profit": "USD",
        "currency_margin": "USD",
        "swap_long": -6.0,
        "swap_short": 1.5,
    },
    "GBPUSD": {
        "description": "British Pound vs US Dollar",
        "volatility": 0.0003,
        "spread_bps": 1.5,
        "trade_contract_size": 100_000.0,
        "digits": 5,
        "point": 0.00001,
        "trade_tick_size": 0.00001,
        "trade_tick_value": 1.0,
        "volume_min": 0.01,
        "volume_max": 200.0,
        "volume_step": 0.01,
        "volume_limit": 0,
        "currency_base": "GBP",
        "currency_profit": "USD",
        "currency_margin": "USD",
        "swap_long": -6.0,
        "swap_short": 1.5,
    },
}

SIM_BASE_PRICES = {
    "BTCUSD": 65_000.0,
    "ETHUSD": 2_600.0,
    "XAUUSD": 2_450.0,
    "EURUSD": 1.085,
    "GBPUSD": 1.27,
}


class SimulatedBroker(Broker):
    """Safe deterministic demo broker used for plumbing and regression tests."""

    def __init__(self, prices: dict[str, float] | None = None):
        self.balance = 10_000.0
        self.equity = 10_000.0
        self.positions: list[Position] = []
        self.pending_orders: list[PendingOrder] = []
        self.recent_trades: list[dict] = []
        self.prices = dict(SIM_BASE_PRICES)
        if prices:
            self.prices.update({str(k).upper(): float(v) for k, v in prices.items()})
        self._rng = random.Random(42)
        self._resolution: dict[str, SymbolResolution] = {}
        self._contracts: dict[str, SymbolContractSpec] = {
            symbol: SymbolContractSpec(
                broker_symbol=symbol,
                canonical_symbol=self._canonical_for(symbol),
                is_tradable=True,
                description=profile["description"],
                point=profile["point"],
                digits=profile["digits"],
                trade_tick_size=profile["trade_tick_size"],
                trade_tick_value=profile["trade_tick_value"],
                trade_tick_value_profit=profile["trade_tick_value"],
                trade_tick_value_loss=profile["trade_tick_value"],
                trade_contract_size=profile["trade_contract_size"],
                volume_min=profile["volume_min"],
                volume_max=profile["volume_max"],
                volume_step=profile["volume_step"],
                volume_limit=profile["volume_limit"],
                trade_stops_level=0,
                trade_freeze_level=0,
                trade_mode=4,
                trade_mode_label="FULL",
                trade_calc_mode=0,
                filling_mode=1,
                order_mode=1,
                expiration_mode=0,
                currency_base=profile["currency_base"],
                currency_profit=profile["currency_profit"],
                currency_margin=profile["currency_margin"],
                swap_mode=0,
                swap_long=profile["swap_long"],
                swap_short=profile["swap_short"],
                swap_rollover3days=3,
            )
            for symbol, profile in SIM_PROFILE.items()
        }
        self._contracts.update(
            self._contracts.get(symbol, SymbolContractSpec(broker_symbol=symbol, canonical_symbol=symbol))
            for symbol in self.prices
            if symbol not in self._contracts
        )

    @staticmethod
    def _canonical_for(broker_symbol: str) -> str:
        from ..symbols import CANONICAL_SYMBOLS

        if broker_symbol in CANONICAL_SYMBOLS:
            return broker_symbol
        matches = [c for c in CANONICAL_SYMBOLS if broker_symbol.startswith(c) and len(broker_symbol) > len(c)]
        return max(matches, key=len, default=broker_symbol)

    async def connect(self) -> None:
        return None

    async def assert_demo(self) -> None:
        return None

    async def resolve_symbols(
        self,
        canonical: Sequence[str],
        symbol_map: Mapping[str, str],
    ) -> dict[str, SymbolResolution]:
        self._resolution = resolve_canonical_symbols(
            [*self.prices.keys(), *SIM_BROKER_SYMBOLS],
            canonical=canonical,
            symbol_map=symbol_map,
        )
        return self._resolution

    async def get_contract(self, canonical: str) -> SymbolContractSpec | None:
        broker_symbol = self._broker_symbol(canonical)
        contract = self._contracts.get(broker_symbol)
        if contract is None:
            return None
        return contract.model_copy(
            update={
                "broker_symbol": broker_symbol,
                "canonical_symbol": canonical,
            }
        )

    def _broker_symbol(self, canonical: str) -> str:
        resolution = self._resolution.get(canonical)
        if resolution is None or not resolution.broker_symbol:
            self._resolution = resolve_canonical_symbols(
                [*self.prices.keys(), *SIM_BROKER_SYMBOLS],
                canonical=[canonical],
                symbol_map={},
            )
            resolution = self._resolution.get(canonical)
        if resolution is None or not resolution.broker_symbol:
            raise RuntimeError(f"Canonical symbol {canonical} is not resolved (status={resolution.status if resolution else 'UNRESOLVED'})")
        return resolution.broker_symbol

    def to_broker_symbol(self, canonical: str) -> str:
        return self._broker_symbol(canonical)

    def _profile(self, broker_symbol: str) -> dict:
        if broker_symbol in SIM_PROFILE:
            return SIM_PROFILE[broker_symbol]
        return {
            "description": broker_symbol,
            "volatility": 0.0007,
            "spread_bps": 5,
            "trade_contract_size": 1.0,
            "digits": 2,
            "point": 0.01,
            "trade_tick_size": 0.01,
            "trade_tick_value": 0.01,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
            "volume_limit": 0,
            "currency_base": broker_symbol[:3],
            "currency_profit": "USD",
            "currency_margin": "USD",
            "swap_long": 0.0,
            "swap_short": 0.0,
        }

    def _tick(self, symbol: str) -> float:
        base = self.prices.get(symbol, 100.0)
        pct = self._profile(symbol)["volatility"]
        base *= 1 + self._rng.gauss(0, pct)
        self.prices[symbol] = max(base, 0.01)
        self._activate_pending(symbol)
        return self.prices[symbol]

    def _spread(self, symbol: str, price: float) -> float:
        return price * self._profile(symbol)["spread_bps"] / 10_000

    def _activate_pending(self, symbol: str) -> None:
        px = self.prices.get(symbol)
        if px is None:
            return
        keep: list[PendingOrder] = []
        for order in self.pending_orders:
            if order.symbol != symbol:
                keep.append(order)
                continue
            trigger = False
            if order.order_type == OrderType.LIMIT:
                trigger = px <= order.price if order.side == Side.LONG else px >= order.price
            elif order.order_type == OrderType.STOP:
                trigger = px >= order.price if order.side == Side.LONG else px <= order.price
            if not trigger:
                keep.append(order)
                continue
            self.positions.append(
                Position(
                    position_id=f"SIM-{uuid4().hex[:10]}",
                    symbol=order.symbol,
                    side=order.side,
                    size=order.size,
                    entry_price=order.price,
                    current_price=px,
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    opened_at=datetime.now(timezone.utc),
                )
            )
            self.recent_trades.append({"kind": "pending_fill", "order_id": order.order_id, "symbol": symbol, "price": px})
        self.pending_orders = keep

    async def get_account_state(self) -> AccountState:
        await self.get_positions()
        floating = sum(p.unrealized_pnl for p in self.positions)
        self.equity = self.balance + floating
        return AccountState(
            account_id="SIM-DEMO",
            is_demo=True,
            balance=self.balance,
            equity=self.equity,
            free_margin=self.equity,
            account_mode="HEDGING",
            hedging_allowed=True,
            daily_pnl=0.0,
            peak_equity=self.equity,
            trailing_drawdown_pct=0.0,
        )

    async def get_quote(self, symbol: str) -> Quote:
        price = self._tick(symbol)
        spread = self._spread(symbol, price)
        return Quote(
            bid=price - spread / 2,
            ask=price + spread / 2,
            spread=spread,
            ts=datetime.now(timezone.utc),
        )

    async def get_market_data(self, symbol: str, timeframes: list[str], limit: int) -> SymbolMarketData:
        quote = await self.get_quote(symbol)
        price = (quote.bid + quote.ask) / 2
        now = quote.ts
        tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
        candles: dict[str, list[Candle]] = {}
        volatility = self._profile(symbol)["volatility"]
        for tf in timeframes:
            minutes = tf_minutes[tf]
            rows: list[Candle] = []
            p = price * (1 - 0.0002 * min(limit, 100))
            for i in range(limit):
                ts = now - timedelta(minutes=minutes * (limit - i))
                drift = math.sin(i / 9) * 0.00015
                move = self._rng.gauss(drift, volatility * 0.6)
                o = p
                c = max(0.01, o * (1 + move))
                wick = abs(self._rng.gauss(0, volatility * 0.35)) * o
                h, l = max(o, c) + wick, min(o, c) - wick
                rows.append(Candle(ts=ts, open=o, high=h, low=l, close=c, volume=abs(self._rng.gauss(100, 30))))
                p = c
            candles[tf] = rows
        canonical = self._canonical_for(symbol)
        contract = self._contracts.get(symbol)
        if contract is not None:
            contract = contract.model_copy(update={"canonical_symbol": canonical})
        return SymbolMarketData(symbol=symbol, quote=quote, candles=candles, contract=contract, market={"source": "simulated", "trade_mode_label": "FULL"})

    async def get_positions(self) -> list[Position]:
        for p in self.positions:
            px = self.prices.get(p.symbol, p.current_price)
            p.current_price = px
            direction = 1 if p.side == Side.LONG else -1
            p.unrealized_pnl = (px - p.entry_price) * direction * p.size
        return [p.model_copy(deep=True) for p in self.positions]

    async def get_pending_orders(self) -> list[PendingOrder]:
        return [o.model_copy(deep=True) for o in self.pending_orders]

    async def get_recent_trades(self) -> list[dict]:
        return list(self.recent_trades[-100:])

    async def estimate_decision_risk(self, decision: Decision) -> float | None:
        if decision.decision not in {DecisionAction.LONG, DecisionAction.SHORT}:
            return 0.0
        if not decision.order or decision.stop_loss is None or not decision.symbol:
            return None
        broker_symbol = self._broker_symbol(decision.symbol)
        quote = await self.get_quote(broker_symbol)
        entry = decision.order.entry
        if decision.order.type == OrderType.MARKET or entry is None:
            entry = quote.ask if decision.decision == DecisionAction.LONG else quote.bid
        return abs(float(entry) - decision.stop_loss) * decision.order.size

    async def estimate_decision_margin(self, decision: Decision) -> float | None:
        # The simulated adapter has no margin engine: report None honestly.
        return None

    async def estimate_open_risk(self) -> float | None:
        risk = 0.0
        for position in self.positions:
            if position.stop_loss is None:
                return None
            risk += abs(position.current_price - position.stop_loss) * position.size
        for order in self.pending_orders:
            if order.stop_loss is None:
                return None
            risk += abs(order.price - order.stop_loss) * order.size
        return risk

    async def execute(self, decision: Decision) -> ExecutionResult:
        if decision.decision == DecisionAction.WAIT:
            return self._result(decision, "SKIPPED", reason="WAIT")
        if decision.decision in {DecisionAction.LONG, DecisionAction.SHORT}:
            return await self._execute_entry(decision)
        if decision.decision == DecisionAction.MANAGE_POSITION:
            return await self._manage(decision)
        if decision.decision == DecisionAction.CLOSE_POSITION:
            return await self._close_position(decision, decision.position_id, 100.0)
        if decision.decision == DecisionAction.CANCEL_ORDER:
            for i, order in enumerate(self.pending_orders):
                if order.order_id == decision.pending_order_id and order.symbol == self._broker_symbol(decision.symbol):
                    self.pending_orders.pop(i)
                    return self._result(decision, "CANCELLED", ticket=order.order_id)
            return self._result(decision, "REJECTED", reason="Pending order not found")
        return self._result(decision, "REJECTED", reason=f"Unsupported action {decision.decision}")

    async def _execute_entry(self, decision: Decision) -> ExecutionResult:
        assert decision.symbol and decision.order and decision.stop_loss is not None
        side = Side.LONG if decision.decision == DecisionAction.LONG else Side.SHORT
        broker_symbol = self._broker_symbol(decision.symbol)
        quote = await self.get_quote(broker_symbol)
        market_px = quote.ask if side == Side.LONG else quote.bid
        profile = self._profile(broker_symbol)
        volume = normalize_volume(decision.order.size, profile["volume_min"], profile["volume_max"], profile["volume_step"])
        if volume <= 0:
            return self._result(decision, "REJECTED", reason=f"Volume below broker minimum {profile['volume_min']}")
        tp = decision.take_profit[0].price if len(decision.take_profit) == 1 and decision.take_profit[0].close_percent >= 99.999 else None
        if decision.order.type != OrderType.MARKET:
            assert decision.order.entry is not None
            ticket = f"SIM-ORD-{uuid4().hex[:10]}"
            self.pending_orders.append(
                PendingOrder(
                    order_id=ticket,
                    symbol=broker_symbol,
                    side=side,
                    order_type=decision.order.type,
                    size=volume,
                    price=decision.order.entry,
                    stop_loss=decision.stop_loss,
                    take_profit=tp,
                    created_at=datetime.now(timezone.utc),
                )
            )
            return self._result(decision, "PENDING", ticket=ticket, requested=decision.order.entry, size=volume, spread=quote.spread)

        ticket = f"SIM-{uuid4().hex[:10]}"
        self.positions.append(
            Position(
                position_id=ticket,
                symbol=broker_symbol,
                side=side,
                size=volume,
                entry_price=market_px,
                current_price=market_px,
                stop_loss=decision.stop_loss,
                take_profit=tp,
                opened_at=datetime.now(timezone.utc),
            )
        )
        return self._result(
            decision,
            "FILLED",
            ticket=ticket,
            requested=decision.order.entry,
            filled=market_px,
            size=volume,
            spread=quote.spread,
            slippage=None if decision.order.entry is None else market_px - decision.order.entry,
        )

    async def _manage(self, decision: Decision) -> ExecutionResult:
        assert decision.management is not None
        instruction = decision.management
        broker_symbol = self._broker_symbol(decision.symbol)
        position = next((p for p in self.positions if p.position_id == instruction.position_id and p.symbol == broker_symbol), None)
        if position is None:
            return self._result(decision, "REJECTED", reason="Position not found")
        if instruction.action == ManagementAction.HOLD:
            return self._result(decision, "SKIPPED", ticket=position.position_id, reason="HOLD")
        if instruction.action == ManagementAction.MOVE_SL:
            assert instruction.stop_loss is not None
            if self._loosens_stop(position, instruction.stop_loss):
                return self._result(decision, "REJECTED", reason="Stop-loss cannot be loosened")
            position.stop_loss = instruction.stop_loss
            return self._result(decision, "MODIFIED", ticket=position.position_id)
        if instruction.action == ManagementAction.MOVE_TP:
            position.take_profit = instruction.take_profit
            return self._result(decision, "MODIFIED", ticket=position.position_id)
        if instruction.action == ManagementAction.BREAK_EVEN:
            if self._loosens_stop(position, position.entry_price):
                return self._result(decision, "REJECTED", reason="Break-even would loosen stop-loss")
            position.stop_loss = position.entry_price
            return self._result(decision, "MODIFIED", ticket=position.position_id)
        if instruction.action == ManagementAction.PARTIAL_CLOSE:
            return await self._close_position(decision, position.position_id, instruction.close_percent or 0)
        if instruction.action == ManagementAction.FULL_CLOSE:
            return await self._close_position(decision, position.position_id, 100.0)
        return self._result(decision, "REJECTED", reason="Unknown management action")

    async def _close_position(self, decision: Decision, position_id: str | None, close_percent: float) -> ExecutionResult:
        broker_symbol = self._broker_symbol(decision.symbol)
        position = next((p for p in self.positions if p.position_id == position_id and p.symbol == broker_symbol), None)
        if position is None:
            return self._result(decision, "REJECTED", reason="Position not found")
        close_size = position.size * close_percent / 100.0
        close_size = min(position.size, max(0.0, close_size))
        if close_size <= 0:
            return self._result(decision, "REJECTED", reason="Invalid close size")
        quote = await self.get_quote(position.symbol)
        px = quote.bid if position.side == Side.LONG else quote.ask
        direction = 1 if position.side == Side.LONG else -1
        realized = (px - position.entry_price) * direction * close_size
        self.balance += realized
        position.size -= close_size
        if position.size <= 1e-12:
            self.positions.remove(position)
            status = "CLOSED"
        else:
            status = "MODIFIED"
        self.recent_trades.append({"kind": "close", "position_id": position_id, "size": close_size, "price": px, "pnl": realized})
        return self._result(decision, status, ticket=position_id, filled=px, size=close_size, spread=quote.spread)

    @staticmethod
    def _loosens_stop(position: Position, new_sl: float) -> bool:
        if position.stop_loss is None:
            return False
        if position.side == Side.LONG:
            return new_sl < position.stop_loss
        return new_sl > position.stop_loss

    @staticmethod
    def _result(
        decision: Decision,
        status: str,
        *,
        ticket: str | None = None,
        requested: float | None = None,
        filled: float | None = None,
        size: float | None = None,
        spread: float | None = None,
        slippage: float | None = None,
        reason: str | None = None,
    ) -> ExecutionResult:
        return ExecutionResult(
            decision_id=decision.decision_id,
            cycle_id=decision.cycle_id,
            status=status,
            broker_ticket=ticket,
            requested_price=requested,
            filled_price=filled,
            actual_size=size,
            spread=spread,
            slippage=slippage,
            reason=reason,
        )