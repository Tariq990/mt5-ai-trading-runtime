from __future__ import annotations

from datetime import datetime, timezone

from .base import Broker
from ..models import (
    AccountState,
    Candle,
    Decision,
    DecisionAction,
    ExecutionResult,
    PendingOrder,
    Position,
    Quote,
    Side,
    SymbolMarketData,
)


class MT5DemoBroker(Broker):
    """MetaTrader 5 adapter with a hard demo-account guard.

    The module imports MetaTrader5 lazily so the rest of GPTTRADDER remains runnable
    on non-Windows development machines.
    """

    TF_MAP_NAMES = {
        "1m": "TIMEFRAME_M1",
        "5m": "TIMEFRAME_M5",
        "15m": "TIMEFRAME_M15",
        "30m": "TIMEFRAME_M30",
        "1h": "TIMEFRAME_H1",
        "4h": "TIMEFRAME_H4",
        "1d": "TIMEFRAME_D1",
    }

    def __init__(self):
        self.mt5 = None
        self._initial_balance: float | None = None
        self._peak_equity: float | None = None

    async def connect(self) -> None:
        try:
            import MetaTrader5 as mt5  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install GPTTRADDER with the 'mt5' extra on Windows.") from exc
        self.mt5 = mt5
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        await self.assert_demo()
        info = mt5.account_info()
        self._initial_balance = float(info.balance)
        self._peak_equity = float(info.equity)

    async def assert_demo(self) -> None:
        if self.mt5 is None:
            raise RuntimeError("MT5 is not connected")
        info = self.mt5.account_info()
        if info is None:
            raise RuntimeError("Unable to read MT5 account info")
        demo_mode = getattr(self.mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
        if int(info.trade_mode) != int(demo_mode):
            raise RuntimeError("REAL ACCOUNT BLOCKED: GPTTRADDER MVP only permits MT5 demo accounts.")

    async def get_account_state(self) -> AccountState:
        await self.assert_demo()
        info = self.mt5.account_info()
        equity = float(info.equity)
        balance = float(info.balance)
        self._initial_balance = self._initial_balance or balance
        self._peak_equity = max(self._peak_equity or equity, equity)
        dd = (self._peak_equity - equity) / max(self._peak_equity, 1e-9) * 100
        daily_pnl = equity - self._initial_balance
        return AccountState(
            account_id=str(info.login),
            is_demo=True,
            balance=balance,
            equity=equity,
            free_margin=float(info.margin_free),
            daily_pnl=daily_pnl,
            peak_equity=self._peak_equity,
            trailing_drawdown_pct=dd,
        )

    async def get_market_data(self, symbol: str, timeframes: list[str], limit: int) -> SymbolMarketData:
        await self.assert_demo()
        mt5 = self.mt5
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"MT5 symbol_select failed for {symbol}")
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"No tick available for {symbol}")
        quote = Quote(
            bid=float(tick.bid),
            ask=float(tick.ask),
            spread=float(tick.ask - tick.bid),
            ts=datetime.fromtimestamp(tick.time, tz=timezone.utc),
        )
        candles: dict[str, list[Candle]] = {}
        for tf in timeframes:
            tf_const = getattr(mt5, self.TF_MAP_NAMES[tf])
            rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, limit)
            if rates is None:
                raise RuntimeError(f"Failed to fetch {symbol} {tf}: {mt5.last_error()}")
            candles[tf] = [
                Candle(
                    ts=datetime.fromtimestamp(int(r["time"]), tz=timezone.utc),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=float(r["tick_volume"]),
                )
                for r in rates
            ]
        return SymbolMarketData(symbol=symbol, quote=quote, candles=candles, market={"source": "mt5"})

    async def get_positions(self) -> list[Position]:
        await self.assert_demo()
        mt5 = self.mt5
        positions = mt5.positions_get() or []
        result = []
        for p in positions:
            long_type = getattr(mt5, "POSITION_TYPE_BUY", 0)
            side = Side.LONG if int(p.type) == int(long_type) else Side.SHORT
            result.append(
                Position(
                    position_id=str(p.ticket),
                    symbol=p.symbol,
                    side=side,
                    size=float(p.volume),
                    entry_price=float(p.price_open),
                    current_price=float(p.price_current),
                    stop_loss=float(p.sl) if p.sl else None,
                    take_profit=float(p.tp) if p.tp else None,
                    unrealized_pnl=float(p.profit),
                    opened_at=datetime.fromtimestamp(int(p.time), tz=timezone.utc),
                )
            )
        return result

    async def get_pending_orders(self) -> list[PendingOrder]:
        return []

    async def get_recent_trades(self) -> list[dict]:
        return []

    async def execute(self, decision: Decision) -> ExecutionResult:
        await self.assert_demo()
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
                reason="Management actions are not enabled in the first MT5 MVP.",
            )
        assert decision.symbol and decision.order
        mt5 = self.mt5
        symbol = decision.symbol
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"No tick for {symbol}")
        is_long = decision.decision == DecisionAction.LONG
        price = float(tick.ask if is_long else tick.bid)
        order_type = mt5.ORDER_TYPE_BUY if is_long else mt5.ORDER_TYPE_SELL
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(decision.order.size),
            "type": order_type,
            "price": price,
            "sl": float(decision.stop_loss),
            "tp": float(decision.take_profit[0].price) if decision.take_profit else 0.0,
            "deviation": 20,
            "magic": 560126,
            "comment": f"GPTTRADDER-DEMO:{str(decision.decision_id)[:8]}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None:
            return ExecutionResult(
                decision_id=decision.decision_id,
                cycle_id=decision.cycle_id,
                status="REJECTED",
                requested_price=price,
                reason=f"MT5 order_send returned None: {mt5.last_error()}",
            )
        done_code = getattr(mt5, "TRADE_RETCODE_DONE", 10009)
        status = "FILLED" if int(result.retcode) == int(done_code) else "REJECTED"
        return ExecutionResult(
            decision_id=decision.decision_id,
            cycle_id=decision.cycle_id,
            status=status,
            broker_ticket=str(getattr(result, "order", "")) or None,
            requested_price=price,
            filled_price=float(getattr(result, "price", price) or price),
            actual_size=float(getattr(result, "volume", decision.order.size) or decision.order.size),
            slippage=float(getattr(result, "price", price) or price) - price,
            spread=float(tick.ask - tick.bid),
            reason=None if status == "FILLED" else str(getattr(result, "comment", "rejected")),
        )
