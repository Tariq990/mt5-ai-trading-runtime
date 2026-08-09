from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

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
from ..timeutil import (
    OFFSET_PROBE_SYMBOLS,
    measure_broker_utc_offset_hours,
    normalize_broker_epoch,
)


class MT5DemoBroker(Broker):
    """MetaTrader 5 adapter with hard demo-only enforcement."""

    TF_MAP_NAMES = {
        "1m": "TIMEFRAME_M1",
        "5m": "TIMEFRAME_M5",
        "15m": "TIMEFRAME_M15",
        "30m": "TIMEFRAME_M30",
        "1h": "TIMEFRAME_H1",
        "4h": "TIMEFRAME_H4",
        "1d": "TIMEFRAME_D1",
    }

    TRADE_MODE_LABELS = {
        0: "DISABLED",
        1: "LONGONLY",
        2: "SHORTONLY",
        3: "CLOSEONLY",
        4: "FULL",
    }

    def __init__(self, server_utc_offset_hours: float | None = None):
        self.mt5 = None
        self._resolution: dict[str, SymbolResolution] = {}
        # None = auto-detect from live tick probes (see timeutil); a configured
        # value is an explicit override that is validated against the measured
        # clock and rejected (fail closed) when it contradicts it by >= 0.5h.
        self.configured_offset_hours = server_utc_offset_hours
        self.server_utc_offset_hours: float | None = None
        self.offset_attribution = "unset"

    def _tick_probe(self, symbol: str) -> float | None:
        if self.mt5 is None:
            return None
        tick = self.mt5.symbol_info_tick(symbol)
        if tick is None or not getattr(tick, "time_msc", None):
            return None
        return float(tick.time_msc) / 1000.0

    def measure_utc_offset(self) -> float | None:
        """Auto-detect broker server clock offset from fresh ticks (DST-safe)."""
        candidates = list(OFFSET_PROBE_SYMBOLS)
        candidates.extend(sorted(self._resolution))
        return measure_broker_utc_offset_hours(self._tick_probe, candidates)

    def _apply_offset_policy(self, measured: float | None, require: bool) -> None:
        configured = self.configured_offset_hours
        if configured is None:
            if measured is None:
                if require:
                    raise RuntimeError(
                        "Unable to measure the broker server UTC offset (no fresh tick probes; "
                        "market fully closed?) and GPTTRADDER_BROKER_SERVER_UTC_OFFSET_HOURS is not "
                        "set. Failing closed: freshness normalization needs a verified clock. "
                        "Set the override explicitly or retry when the market is open."
                    )
                return
            self.server_utc_offset_hours = measured
            self.offset_attribution = f"auto-measured {measured:g}h"
            return
        if measured is not None and abs(configured - measured) >= 0.5:
            raise RuntimeError(
                f"Configured broker server UTC offset {configured:g}h contradicts the measured "
                f"{measured:g}h (see GPTTRADDER_BROKER_SERVER_UTC_OFFSET_HOURS). Failing closed: "
                "refusing to normalize timestamps with an implausible offset."
            )
        self.server_utc_offset_hours = configured
        self.offset_attribution = (
            f"configured override {configured:g}h"
            + ("" if measured is None else f" (consistent with measured {measured:g}h)")
        )

    def _utc(self, epoch_seconds: float) -> datetime:
        """MT5 server clock -> canonical UTC (server clock is usually EET/EEST,
        not UTC; see timeutil.normalize_broker_epoch)."""
        if self.server_utc_offset_hours is None:
            raise RuntimeError("MT5 server UTC offset is unverified; refusing to normalize timestamps")
        return normalize_broker_epoch(epoch_seconds, self.server_utc_offset_hours)

    def _server_epoch(self, utc_dt: datetime) -> float:
        if self.server_utc_offset_hours is None:
            raise RuntimeError("MT5 server UTC offset is unverified; refusing to convert to server time")
        return (utc_dt.astimezone(timezone.utc).timestamp() + self.server_utc_offset_hours * 3600.0)

    async def connect(self) -> None:
        try:
            import MetaTrader5 as mt5  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install GPTTRADDER with the 'mt5' extra on Windows.") from exc
        self.mt5 = mt5
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        await self.assert_demo()
        self._apply_offset_policy(self.measure_utc_offset(), require=True)

    async def assert_demo(self) -> None:
        if self.mt5 is None:
            raise RuntimeError("MT5 is not connected")
        info = self.mt5.account_info()
        if info is None:
            raise RuntimeError("Unable to read MT5 account info")
        demo_mode = getattr(self.mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
        if int(info.trade_mode) != int(demo_mode):
            raise RuntimeError("REAL ACCOUNT BLOCKED: GPTTRADDER only permits MT5 demo accounts.")

    async def resolve_symbols(
        self,
        canonical: Sequence[str],
        symbol_map: Mapping[str, str],
    ) -> dict[str, SymbolResolution]:
        await self.assert_demo()
        symbols = self.mt5.symbols_get() or []
        names = {str(s.name).strip().upper() for s in symbols if getattr(s, "name", None)}
        self._resolution = resolve_canonical_symbols(names, canonical=canonical, symbol_map=symbol_map)
        # Re-measure the broker clock on every cycle: DST shifts (EET<->EEST)
        # are picked up automatically, and any contradiction with an explicit
        # override fails closed immediately.
        self._apply_offset_policy(self.measure_utc_offset(), require=self.configured_offset_hours is None)
        return self._resolution

    async def get_contract(self, canonical: str) -> SymbolContractSpec | None:
        broker_symbol = self._broker_symbol(canonical)
        return self._build_contract(broker_symbol, canonical=canonical)

    def _broker_symbol(self, canonical: str) -> str:
        resolution = self._resolution.get(canonical)
        if resolution is not None and resolution.broker_symbol:
            return resolution.broker_symbol
        raise RuntimeError(f"Canonical symbol {canonical} is not resolved (status={resolution.status if resolution else 'UNRESOLVED'})")

    def to_broker_symbol(self, canonical: str) -> str:
        return self._broker_symbol(canonical)

    def _build_contract(self, broker_symbol: str, canonical: str | None = None) -> SymbolContractSpec | None:
        mt5 = self.mt5
        info = mt5.symbol_info(broker_symbol)
        if info is None:
            return None
        if canonical is None:
            canonical = self.broker_symbol_to_canonical().get(broker_symbol, broker_symbol)
        trade_mode = int(getattr(info, "trade_mode", -1))
        return SymbolContractSpec(
            broker_symbol=broker_symbol,
            canonical_symbol=canonical,
            is_tradable=trade_mode not in {
                int(getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", 0)),
                int(getattr(mt5, "SYMBOL_TRADE_MODE_CLOSEONLY", 3)),
            },
            description=str(getattr(info, "description", None) or None),
            path=str(getattr(info, "path", None) or None),
            point=float(getattr(info, "point", 0.0) or 0.0) or None,
            digits=int(getattr(info, "digits", -1)) if int(getattr(info, "digits", -1)) >= 0 else None,
            trade_tick_size=float(getattr(info, "trade_tick_size", 0.0) or 0.0) or None,
            trade_tick_value=float(getattr(info, "trade_tick_value", 0.0) or 0.0) or None,
            trade_tick_value_profit=float(getattr(info, "trade_tick_value_profit", 0.0) or 0.0) or None,
            trade_tick_value_loss=float(getattr(info, "trade_tick_value_loss", 0.0) or 0.0) or None,
            trade_contract_size=float(getattr(info, "trade_contract_size", 0.0) or 0.0) or None,
            volume_min=float(getattr(info, "volume_min", 0.0) or 0.0) or None,
            volume_max=float(getattr(info, "volume_max", 0.0) or 0.0) or None,
            volume_step=float(getattr(info, "volume_step", 0.0) or 0.0) or None,
            volume_limit=float(getattr(info, "volume_limit", 0.0) or 0.0) or None,
            trade_stops_level=int(getattr(info, "trade_stops_level", -1)) if int(getattr(info, "trade_stops_level", -1)) >= 0 else None,
            trade_freeze_level=int(getattr(info, "trade_freeze_level", -1)) if int(getattr(info, "trade_freeze_level", -1)) >= 0 else None,
            trade_mode=trade_mode,
            trade_mode_label=self.TRADE_MODE_LABELS.get(trade_mode, "UNKNOWN"),
            trade_calc_mode=int(getattr(info, "trade_calc_mode", -1)) if int(getattr(info, "trade_calc_mode", -1)) >= 0 else None,
            filling_mode=int(getattr(info, "filling_mode", -1)) if int(getattr(info, "filling_mode", -1)) >= 0 else None,
            order_mode=int(getattr(info, "order_mode", -1)) if int(getattr(info, "order_mode", -1)) >= 0 else None,
            expiration_mode=int(getattr(info, "expiration_mode", -1)) if int(getattr(info, "expiration_mode", -1)) >= 0 else None,
            trade_exemode=int(getattr(info, "trade_exemode", -1)) if int(getattr(info, "trade_exemode", -1)) >= 0 else None,
            currency_base=str(getattr(info, "currency_base", None) or None),
            currency_profit=str(getattr(info, "currency_profit", None) or None),
            currency_margin=str(getattr(info, "currency_margin", None) or None),
            swap_mode=int(getattr(info, "swap_mode", -1)) if int(getattr(info, "swap_mode", -1)) >= 0 else None,
            swap_long=float(getattr(info, "swap_long", 0.0) or 0.0) or None,
            swap_short=float(getattr(info, "swap_short", 0.0) or 0.0) or None,
            swap_rollover3days=int(getattr(info, "swap_rollover3days", -1)) if int(getattr(info, "swap_rollover3days", -1)) >= 0 else None,
            margin_initial=float(getattr(info, "margin_initial", 0.0) or 0.0) or None,
            margin_maintenance=float(getattr(info, "margin_maintenance", 0.0) or 0.0) or None,
        )

    async def get_account_state(self) -> AccountState:
        await self.assert_demo()
        info = self.mt5.account_info()
        equity = float(info.equity)
        margin_mode = int(getattr(info, "margin_mode", -1))
        hedging_mode = int(getattr(self.mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2))
        netting_mode = int(getattr(self.mt5, "ACCOUNT_MARGIN_MODE_RETAIL_NETTING", 0))
        exchange_mode = int(getattr(self.mt5, "ACCOUNT_MARGIN_MODE_EXCHANGE", 1))
        account_mode = {
            hedging_mode: "HEDGING",
            netting_mode: "NETTING",
            exchange_mode: "EXCHANGE",
        }.get(margin_mode, f"UNKNOWN:{margin_mode}")
        return AccountState(
            account_id=str(info.login),
            is_demo=True,
            balance=float(info.balance),
            equity=equity,
            free_margin=float(info.margin_free),
            account_mode=account_mode,
            hedging_allowed=margin_mode == hedging_mode,
            # Durable daily baseline + peak are applied by Database.apply_risk_state().
            daily_pnl=0.0,
            peak_equity=equity,
            trailing_drawdown_pct=0.0,
        )

    async def get_quote(self, symbol: str) -> Quote:
        await self.assert_demo()
        mt5 = self.mt5
        self._ensure_symbol(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"No tick available for {symbol}: {mt5.last_error()}")
        ts_raw = getattr(tick, "time_msc", 0)
        ts = self._utc(ts_raw / 1000.0 if ts_raw else float(tick.time))
        return Quote(bid=float(tick.bid), ask=float(tick.ask), spread=float(tick.ask - tick.bid), ts=ts)

    async def get_market_data(self, symbol: str, timeframes: list[str], limit: int) -> SymbolMarketData:
        await self.assert_demo()
        mt5 = self.mt5
        self._ensure_symbol(symbol)
        quote = await self.get_quote(symbol)
        candles: dict[str, list[Candle]] = {}
        for tf in timeframes:
            tf_const = getattr(mt5, self.TF_MAP_NAMES[tf])
            rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, limit)
            if rates is None:
                raise RuntimeError(f"Failed to fetch {symbol} {tf}: {mt5.last_error()}")
            candles[tf] = [
                Candle(
                    ts=self._utc(int(r["time"])),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=float(r["tick_volume"]),
                )
                for r in rates
            ]
        info = mt5.symbol_info(symbol)
        trade_mode = int(getattr(info, "trade_mode", -1)) if info is not None else -1
        trade_mode_labels = {
            int(getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", 0)): "DISABLED",
            int(getattr(mt5, "SYMBOL_TRADE_MODE_LONGONLY", 1)): "LONGONLY",
            int(getattr(mt5, "SYMBOL_TRADE_MODE_SHORTONLY", 2)): "SHORTONLY",
            int(getattr(mt5, "SYMBOL_TRADE_MODE_CLOSEONLY", 3)): "CLOSEONLY",
            int(getattr(mt5, "SYMBOL_TRADE_MODE_FULL", 4)): "FULL",
        }
        market: dict[str, Any] = {
            "source": "mt5",
            "trade_mode": trade_mode,
            "trade_mode_label": trade_mode_labels.get(trade_mode, "UNKNOWN"),
            "trade_exemode": int(getattr(info, "trade_exemode", -1)) if info is not None else -1,
            "ticks_bookdepth": int(getattr(info, "ticks_bookdepth", 0)) if info is not None else 0,
        }
        # DOM is optional: many CFD brokers do not expose it for every symbol.
        try:
            if mt5.market_book_add(symbol):
                try:
                    book = mt5.market_book_get(symbol) or []
                    market["dom"] = [
                        {
                            "type": int(getattr(item, "type", 0)),
                            "price": float(getattr(item, "price", 0.0)),
                            "volume": float(getattr(item, "volume_dbl", getattr(item, "volume", 0.0))),
                        }
                        for item in book
                    ]
                finally:
                    mt5.market_book_release(symbol)
        except Exception:
            market["dom"] = None
        return SymbolMarketData(symbol=symbol, quote=quote, candles=candles, market=market, contract=self._build_contract(symbol))

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
                    opened_at=self._utc(int(p.time)),
                )
            )
        return result

    async def get_pending_orders(self) -> list[PendingOrder]:
        await self.assert_demo()
        mt5 = self.mt5
        orders = mt5.orders_get() or []
        result: list[PendingOrder] = []
        buy_types = {
            int(getattr(mt5, "ORDER_TYPE_BUY_LIMIT", 2)),
            int(getattr(mt5, "ORDER_TYPE_BUY_STOP", 4)),
            int(getattr(mt5, "ORDER_TYPE_BUY_STOP_LIMIT", 6)),
        }
        limit_types = {
            int(getattr(mt5, "ORDER_TYPE_BUY_LIMIT", 2)),
            int(getattr(mt5, "ORDER_TYPE_SELL_LIMIT", 3)),
        }
        for order in orders:
            order_type_value = int(order.type)
            if order_type_value in {
                int(getattr(mt5, "ORDER_TYPE_BUY", 0)),
                int(getattr(mt5, "ORDER_TYPE_SELL", 1)),
            }:
                continue
            result.append(
                PendingOrder(
                    order_id=str(order.ticket),
                    symbol=order.symbol,
                    side=Side.LONG if order_type_value in buy_types else Side.SHORT,
                    order_type=OrderType.LIMIT if order_type_value in limit_types else OrderType.STOP,
                    size=float(order.volume_current or order.volume_initial),
                    price=float(order.price_open),
                    stop_loss=float(order.sl) if order.sl else None,
                    take_profit=float(order.tp) if order.tp else None,
                    created_at=self._utc(int(order.time_setup)),
                )
            )
        return result

    async def get_recent_trades(self) -> list[dict]:
        await self.assert_demo()
        mt5 = self.mt5
        # MT5 history queries run in server time; convert the UTC window back
        # to the broker clock so the last 24h are not silently shifted.
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=24)
        deals = mt5.history_deals_get(
            datetime.fromtimestamp(self._server_epoch(start), tz=timezone.utc),
            datetime.fromtimestamp(self._server_epoch(end), tz=timezone.utc),
        ) or []
        fields = ("ticket", "order", "time", "type", "entry", "position_id", "volume", "price", "profit", "symbol", "comment")
        rows = []
        for deal in deals[-100:]:
            rows.append({field: getattr(deal, field, None) for field in fields})
        return rows

    async def estimate_decision_risk(self, decision: Decision) -> float | None:
        if decision.decision not in {DecisionAction.LONG, DecisionAction.SHORT}:
            return 0.0
        if not decision.symbol or not decision.order or decision.stop_loss is None:
            return None
        mt5 = self.mt5
        symbol = self._broker_symbol(decision.symbol)
        quote = await self.get_quote(symbol)
        entry = decision.order.entry
        if decision.order.type == OrderType.MARKET or entry is None:
            entry = quote.ask if decision.decision == DecisionAction.LONG else quote.bid
        side_type = mt5.ORDER_TYPE_BUY if decision.decision == DecisionAction.LONG else mt5.ORDER_TYPE_SELL
        value = mt5.order_calc_profit(side_type, symbol, float(decision.order.size), float(entry), float(decision.stop_loss))
        return None if value is None else abs(float(value))

    async def estimate_decision_margin(self, decision: Decision) -> float | None:
        if decision.decision not in {DecisionAction.LONG, DecisionAction.SHORT}:
            return None
        if not decision.symbol or not decision.order:
            return None
        mt5 = self.mt5
        symbol = self._broker_symbol(decision.symbol)
        volume = self._normalize_volume(symbol, decision.order.size)
        if volume <= 0:
            return None
        quote = await self.get_quote(symbol)
        entry = decision.order.entry
        if decision.order.type == OrderType.MARKET or entry is None:
            entry = quote.ask if decision.decision == DecisionAction.LONG else quote.bid
        side_type = mt5.ORDER_TYPE_BUY if decision.decision == DecisionAction.LONG else mt5.ORDER_TYPE_SELL
        value = mt5.order_calc_margin(side_type, symbol, volume, float(entry))
        return None if value is None else abs(float(value))

    async def estimate_open_risk(self) -> float | None:
        mt5 = self.mt5
        total = 0.0
        for position in await self.get_positions():
            if position.stop_loss is None:
                return None
            side_type = mt5.ORDER_TYPE_BUY if position.side == Side.LONG else mt5.ORDER_TYPE_SELL
            value = mt5.order_calc_profit(
                side_type,
                position.symbol,
                position.size,
                position.current_price,
                position.stop_loss,
            )
            if value is None:
                return None
            total += abs(float(value))
        for order in await self.get_pending_orders():
            if order.stop_loss is None:
                return None
            side_type = mt5.ORDER_TYPE_BUY if order.side == Side.LONG else mt5.ORDER_TYPE_SELL
            value = mt5.order_calc_profit(side_type, order.symbol, order.size, order.price, order.stop_loss)
            if value is None:
                return None
            total += abs(float(value))
        return total

    async def execute(self, decision: Decision) -> ExecutionResult:
        await self.assert_demo()
        if decision.decision == DecisionAction.WAIT:
            return self._result(decision, "SKIPPED", reason="WAIT")
        if decision.decision in {DecisionAction.LONG, DecisionAction.SHORT}:
            return await self._execute_entry(decision)
        if decision.decision == DecisionAction.MANAGE_POSITION:
            return await self._manage_position(decision)
        if decision.decision == DecisionAction.CLOSE_POSITION:
            return await self._close_position(decision, decision.position_id, 100.0)
        if decision.decision == DecisionAction.CANCEL_ORDER:
            return await self._cancel_order(decision)
        return self._result(decision, "REJECTED", reason=f"Unsupported action {decision.decision}")

    async def _execute_entry(self, decision: Decision) -> ExecutionResult:
        assert decision.symbol and decision.order and decision.stop_loss is not None
        mt5 = self.mt5
        symbol = self._broker_symbol(decision.symbol)
        self._ensure_symbol(symbol)
        quote = await self.get_quote(symbol)
        is_long = decision.decision == DecisionAction.LONG
        market_price = quote.ask if is_long else quote.bid
        volume = self._normalize_volume(symbol, decision.order.size)
        broker_tp = self._broker_take_profit(decision)

        if decision.order.type == OrderType.MARKET:
            request: dict[str, Any] = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": mt5.ORDER_TYPE_BUY if is_long else mt5.ORDER_TYPE_SELL,
                "price": market_price,
                "sl": float(decision.stop_loss),
                "tp": broker_tp,
                "deviation": 20,
                "magic": 560126,
                "comment": f"GPTTRADDER-DEMO:{str(decision.decision_id)[:8]}",
                "type_time": mt5.ORDER_TIME_GTC,
            }
        else:
            assert decision.order.entry is not None
            if decision.order.type == OrderType.LIMIT:
                order_type = mt5.ORDER_TYPE_BUY_LIMIT if is_long else mt5.ORDER_TYPE_SELL_LIMIT
            else:
                order_type = mt5.ORDER_TYPE_BUY_STOP if is_long else mt5.ORDER_TYPE_SELL_STOP
            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": symbol,
                "volume": volume,
                "type": order_type,
                "price": float(decision.order.entry),
                "sl": float(decision.stop_loss),
                "tp": broker_tp,
                "magic": 560126,
                "comment": f"GPTTRADDER-DEMO:{str(decision.decision_id)[:8]}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": getattr(mt5, "ORDER_FILLING_RETURN", 2),
            }

        result = self._checked_send(request, symbol=symbol, market_order=decision.order.type == OrderType.MARKET)
        if isinstance(result, str):
            return self._result(decision, "REJECTED", requested=request.get("price"), spread=quote.spread, reason=result)
        done_codes = {
            int(getattr(mt5, "TRADE_RETCODE_DONE", 10009)),
            int(getattr(mt5, "TRADE_RETCODE_PLACED", 10008)),
            int(getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010)),
        }
        ok = int(result.retcode) in done_codes
        if decision.order.type == OrderType.MARKET:
            status = "FILLED" if ok else "REJECTED"
        else:
            status = "PENDING" if ok else "REJECTED"
        fill_price = float(getattr(result, "price", 0.0) or 0.0) or (market_price if status == "FILLED" else None)
        ticket_value = getattr(result, "order", 0) or getattr(result, "deal", 0)
        return self._result(
            decision,
            status,
            ticket=str(ticket_value) if ticket_value else None,
            requested=float(request.get("price", 0.0)) or None,
            filled=fill_price,
            size=float(getattr(result, "volume", 0.0) or volume),
            slippage=(fill_price - market_price) if fill_price is not None and decision.order.type == OrderType.MARKET else None,
            spread=quote.spread,
            reason=None if ok else str(getattr(result, "comment", "rejected")),
        )

    async def _manage_position(self, decision: Decision) -> ExecutionResult:
        assert decision.management is not None and decision.symbol
        instruction = decision.management
        position = self._find_position(instruction.position_id, self._broker_symbol(decision.symbol))
        if position is None:
            return self._result(decision, "REJECTED", reason="Position not found")
        if instruction.action == ManagementAction.HOLD:
            return self._result(decision, "SKIPPED", ticket=str(position.ticket), reason="HOLD")
        if instruction.action == ManagementAction.PARTIAL_CLOSE:
            return await self._close_position(decision, instruction.position_id, instruction.close_percent or 0.0)
        if instruction.action == ManagementAction.FULL_CLOSE:
            return await self._close_position(decision, instruction.position_id, 100.0)

        old_sl = float(position.sl or 0.0) or None
        old_tp = float(position.tp or 0.0) or None
        long_type = int(getattr(self.mt5, "POSITION_TYPE_BUY", 0))
        side = Side.LONG if int(position.type) == long_type else Side.SHORT
        if instruction.action == ManagementAction.MOVE_SL:
            assert instruction.stop_loss is not None
            new_sl = float(instruction.stop_loss)
            if old_sl is not None and ((side == Side.LONG and new_sl < old_sl) or (side == Side.SHORT and new_sl > old_sl)):
                return self._result(decision, "REJECTED", reason="Stop-loss cannot be loosened")
            new_tp = old_tp or 0.0
        elif instruction.action == ManagementAction.BREAK_EVEN:
            new_sl = float(position.price_open)
            if old_sl is not None and ((side == Side.LONG and new_sl < old_sl) or (side == Side.SHORT and new_sl > old_sl)):
                return self._result(decision, "REJECTED", reason="Break-even would loosen stop-loss")
            new_tp = old_tp or 0.0
        elif instruction.action == ManagementAction.MOVE_TP:
            new_sl = old_sl or 0.0
            new_tp = float(instruction.take_profit)
        else:
            return self._result(decision, "REJECTED", reason="Unsupported management action")

        request = {
            "action": self.mt5.TRADE_ACTION_SLTP,
            "position": int(position.ticket),
            "symbol": position.symbol,
            "sl": new_sl,
            "tp": new_tp,
            "magic": 560126,
            "comment": f"GPTTRADDER-DEMO:{str(decision.decision_id)[:8]}",
        }
        result = self.mt5.order_send(request)
        if result is None:
            return self._result(decision, "REJECTED", ticket=str(position.ticket), reason=f"MT5 order_send None: {self.mt5.last_error()}")
        ok = int(result.retcode) == int(getattr(self.mt5, "TRADE_RETCODE_DONE", 10009))
        return self._result(
            decision,
            "MODIFIED" if ok else "REJECTED",
            ticket=str(position.ticket),
            reason=None if ok else str(getattr(result, "comment", "rejected")),
        )

    async def _close_position(self, decision: Decision, position_id: str | None, close_percent: float) -> ExecutionResult:
        if not position_id or not decision.symbol:
            return self._result(decision, "REJECTED", reason="Missing position_id/symbol")
        position = self._find_position(position_id, self._broker_symbol(decision.symbol))
        if position is None:
            return self._result(decision, "REJECTED", reason="Position not found")
        mt5 = self.mt5
        quote = await self.get_quote(position.symbol)
        long_type = int(getattr(mt5, "POSITION_TYPE_BUY", 0))
        is_long = int(position.type) == long_type
        volume = self._normalize_volume(position.symbol, float(position.volume) * close_percent / 100.0, clamp_to_position=float(position.volume))
        if volume <= 0:
            return self._result(decision, "REJECTED", reason="Close volume below broker minimum")
        price = quote.bid if is_long else quote.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": int(position.ticket),
            "symbol": position.symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY,
            "price": price,
            "deviation": 20,
            "magic": 560126,
            "comment": f"GPTTRADDER-DEMO:{str(decision.decision_id)[:8]}",
            "type_time": mt5.ORDER_TIME_GTC,
        }
        result = self._checked_send(request, symbol=position.symbol, market_order=True)
        if isinstance(result, str):
            return self._result(decision, "REJECTED", ticket=str(position.ticket), requested=price, spread=quote.spread, reason=result)
        done_codes = {
            int(getattr(mt5, "TRADE_RETCODE_DONE", 10009)),
            int(getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010)),
        }
        ok = int(result.retcode) in done_codes
        closed_all = math.isclose(volume, float(position.volume), rel_tol=0, abs_tol=1e-12)
        return self._result(
            decision,
            ("CLOSED" if closed_all else "MODIFIED") if ok else "REJECTED",
            ticket=str(position.ticket),
            requested=price,
            filled=float(getattr(result, "price", 0.0) or price),
            size=float(getattr(result, "volume", 0.0) or volume),
            spread=quote.spread,
            reason=None if ok else str(getattr(result, "comment", "rejected")),
        )

    async def _cancel_order(self, decision: Decision) -> ExecutionResult:
        if not decision.pending_order_id:
            return self._result(decision, "REJECTED", reason="Missing pending_order_id")
        orders = self.mt5.orders_get(ticket=int(decision.pending_order_id)) or []
        if not orders:
            return self._result(decision, "REJECTED", reason="Pending order not found")
        order = orders[0]
        if decision.symbol and order.symbol != self._broker_symbol(decision.symbol):
            return self._result(decision, "REJECTED", reason="Order symbol mismatch")
        result = self.mt5.order_send({"action": self.mt5.TRADE_ACTION_REMOVE, "order": int(order.ticket)})
        if result is None:
            return self._result(decision, "REJECTED", reason=f"MT5 order_send None: {self.mt5.last_error()}")
        ok = int(result.retcode) == int(getattr(self.mt5, "TRADE_RETCODE_DONE", 10009))
        return self._result(
            decision,
            "CANCELLED" if ok else "REJECTED",
            ticket=str(order.ticket),
            reason=None if ok else str(getattr(result, "comment", "rejected")),
        )

    def _checked_send(self, request: dict[str, Any], *, symbol: str, market_order: bool):
        mt5 = self.mt5
        if market_order:
            candidates = [
                getattr(mt5, "ORDER_FILLING_IOC", 1),
                getattr(mt5, "ORDER_FILLING_FOK", 0),
                getattr(mt5, "ORDER_FILLING_RETURN", 2),
            ]
        else:
            candidates = [getattr(mt5, "ORDER_FILLING_RETURN", 2)]
        seen = set()
        last_reason = "order_check rejected request"
        for filling in candidates:
            if filling in seen:
                continue
            seen.add(filling)
            candidate = dict(request)
            candidate["type_filling"] = filling
            check = mt5.order_check(candidate)
            if check is None:
                last_reason = f"MT5 order_check returned None: {mt5.last_error()}"
                continue
            if int(getattr(check, "retcode", -1)) != 0:
                last_reason = str(getattr(check, "comment", f"order_check retcode={check.retcode}"))
                continue
            result = mt5.order_send(candidate)
            if result is not None:
                return result
            last_reason = f"MT5 order_send returned None: {mt5.last_error()}"
        return last_reason

    def _find_position(self, position_id: str, symbol: str):
        rows = self.mt5.positions_get(ticket=int(position_id)) or []
        if not rows:
            return None
        position = rows[0]
        return position if position.symbol == symbol else None

    def _ensure_symbol(self, symbol: str) -> None:
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"MT5 symbol not found: {symbol}")
        if not info.visible and not self.mt5.symbol_select(symbol, True):
            raise RuntimeError(f"MT5 symbol_select failed for {symbol}: {self.mt5.last_error()}")

    def _normalize_volume(self, symbol: str, requested: float, clamp_to_position: float | None = None) -> float:
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"MT5 symbol not found: {symbol}")
        maximum = min(float(info.volume_max), clamp_to_position if clamp_to_position is not None else float(info.volume_max))
        return normalize_volume(requested, float(info.volume_min), maximum, float(info.volume_step))

    @staticmethod
    def _broker_take_profit(decision: Decision) -> float:
        if len(decision.take_profit) == 1 and decision.take_profit[0].close_percent >= 99.999:
            return float(decision.take_profit[0].price)
        return 0.0

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
