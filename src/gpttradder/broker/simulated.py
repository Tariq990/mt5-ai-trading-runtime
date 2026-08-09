from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .base import Broker
from ..models import (
    AccountState,
    Candle,
    Decision,
    DecisionAction,
    ExecutionResult,
    Position,
    Quote,
    Side,
    SymbolMarketData,
)


class SimulatedBroker(Broker):
    """A deterministic-enough demo broker for end-to-end plumbing tests."""

    def __init__(self):
        self.balance = 10_000.0
        self.equity = 10_000.0
        self.peak_equity = 10_000.0
        self.positions: list[Position] = []
        self.prices = {"BTCUSD": 65_000.0, "XAUUSD": 2_450.0}
        self._rng = random.Random(42)

    async def connect(self) -> None:
        return None

    async def assert_demo(self) -> None:
        return None

    def _tick(self, symbol: str) -> float:
        base = self.prices.get(symbol, 100.0)
        pct = 0.0012 if symbol == "BTCUSD" else 0.0007
        base *= 1 + self._rng.gauss(0, pct)
        self.prices[symbol] = max(base, 0.01)
        return self.prices[symbol]

    async def get_account_state(self) -> AccountState:
        floating = sum(p.unrealized_pnl for p in self.positions)
        self.equity = self.balance + floating
        self.peak_equity = max(self.peak_equity, self.equity)
        dd = 0.0 if self.peak_equity <= 0 else (self.peak_equity - self.equity) / self.peak_equity * 100
        return AccountState(
            account_id="SIM-DEMO",
            is_demo=True,
            balance=self.balance,
            equity=self.equity,
            free_margin=self.equity,
            daily_pnl=self.equity - 10_000.0,
            peak_equity=self.peak_equity,
            trailing_drawdown_pct=dd,
        )

    async def get_market_data(self, symbol: str, timeframes: list[str], limit: int) -> SymbolMarketData:
        price = self._tick(symbol)
        spread = price * (0.00008 if symbol == "BTCUSD" else 0.00005)
        now = datetime.now(timezone.utc)
        quote = Quote(bid=price - spread / 2, ask=price + spread / 2, spread=spread, ts=now)
        tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
        candles: dict[str, list[Candle]] = {}
        for tf in timeframes:
            minutes = tf_minutes[tf]
            rows: list[Candle] = []
            p = price * (1 - 0.0002 * min(limit, 100))
            for i in range(limit):
                ts = now - timedelta(minutes=minutes * (limit - i))
                drift = math.sin(i / 9) * 0.00015
                move = self._rng.gauss(drift, 0.0008 if symbol == "BTCUSD" else 0.00045)
                o = p
                c = max(0.01, o * (1 + move))
                wick = abs(self._rng.gauss(0, 0.0004)) * o
                h, l = max(o, c) + wick, min(o, c) - wick
                rows.append(Candle(ts=ts, open=o, high=h, low=l, close=c, volume=abs(self._rng.gauss(100, 30))))
                p = c
            candles[tf] = rows
        return SymbolMarketData(symbol=symbol, quote=quote, candles=candles, market={"source": "simulated"})

    async def get_positions(self) -> list[Position]:
        for p in self.positions:
            px = self.prices.get(p.symbol, p.current_price)
            p.current_price = px
            direction = 1 if p.side == Side.LONG else -1
            p.unrealized_pnl = (px - p.entry_price) * direction * p.size
        return list(self.positions)

    async def get_pending_orders(self):
        return []

    async def get_recent_trades(self):
        return []

    async def execute(self, decision: Decision) -> ExecutionResult:
        if decision.decision == DecisionAction.WAIT:
            return ExecutionResult(
                decision_id=decision.decision_id,
                cycle_id=decision.cycle_id,
                status="SKIPPED",
                reason="WAIT",
            )
        if decision.decision not in {DecisionAction.LONG, DecisionAction.SHORT}:
            return ExecutionResult(
                decision_id=decision.decision_id,
                cycle_id=decision.cycle_id,
                status="SKIPPED",
                reason=f"Action {decision.decision} not implemented by simulated MVP",
            )
        assert decision.symbol and decision.order
        px = self.prices[decision.symbol]
        side = Side.LONG if decision.decision == DecisionAction.LONG else Side.SHORT
        ticket = f"SIM-{uuid4().hex[:10]}"
        self.positions.append(
            Position(
                position_id=ticket,
                symbol=decision.symbol,
                side=side,
                size=decision.order.size,
                entry_price=px,
                current_price=px,
                stop_loss=decision.stop_loss,
                take_profit=decision.take_profit[0].price if decision.take_profit else None,
                opened_at=datetime.now(timezone.utc),
            )
        )
        return ExecutionResult(
            decision_id=decision.decision_id,
            cycle_id=decision.cycle_id,
            status="FILLED",
            broker_ticket=ticket,
            requested_price=decision.order.entry,
            filled_price=px,
            actual_size=decision.order.size,
            slippage=None if decision.order.entry is None else px - decision.order.entry,
            spread=0.0,
        )
