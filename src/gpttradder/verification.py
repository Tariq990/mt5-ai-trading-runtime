from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .broker.mt5_demo import MT5DemoBroker
from .config import Settings
from .models import Decision, DecisionAction, OrderInstruction, OrderType


async def verify_mt5_demo_write(
    settings: Settings,
    broker: MT5DemoBroker,
    symbol_filter: str | None = None,
) -> dict:
    """Place then immediately cancel a far-away minimum-volume pending order.

    This is a DEMO-only operational diagnostic. It verifies the broker write path
    without intentionally opening market exposure. The adapter's demo guard runs
    before both writes. A broker can still fill any pending order during an extreme
    move, so the command should only ever be used on a Demo account.

    `symbol_filter` selects a canonical symbol (e.g. BTC or EURUSD); by default
    every configured canonical symbol is tried in order until one succeeds.
    """
    await broker.assert_demo()
    mt5 = broker.mt5
    if mt5 is None:
        raise RuntimeError("MT5 is not connected")

    resolution = await broker.resolve_symbols(settings.symbols, settings.symbol_map)
    if symbol_filter:
        symbol_filter = str(symbol_filter).strip().upper()
        candidates = [(canonical, res) for canonical, res in resolution.items() if canonical == symbol_filter]
    else:
        candidates = sorted(resolution.items())

    last_error = "No configured symbol accepts a diagnostic pending order"
    for canonical, res in candidates:
        if res.status != "OK" or not res.broker_symbol:
            last_error = f"{canonical}: unresolved ({res.status})"
            continue
        symbol = res.broker_symbol
        try:
            info = mt5.symbol_info(symbol)
            if info is None:
                last_error = f"Symbol not found: {symbol}"
                continue
            trade_mode = int(getattr(info, "trade_mode", getattr(mt5, "SYMBOL_TRADE_MODE_FULL", 4)))
            disabled = int(getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", 0))
            close_only = int(getattr(mt5, "SYMBOL_TRADE_MODE_CLOSEONLY", 3))
            short_only = int(getattr(mt5, "SYMBOL_TRADE_MODE_SHORTONLY", 2))
            if trade_mode in {disabled, close_only}:
                last_error = f"{symbol} is not open for new entries"
                continue

            quote = await broker.get_quote(symbol)
            point = float(getattr(info, "point", 0.0) or max(abs(quote.ask) * 1e-6, 1e-8))
            stops_level = float(getattr(info, "trade_stops_level", 0.0) or 0.0) * point
            distance = max(abs(quote.ask) * 0.10, quote.spread * 100.0, stops_level * 5.0, point * 100.0)
            volume = float(getattr(info, "volume_min", 0.01) or 0.01)

            if trade_mode == short_only:
                action = DecisionAction.SHORT
                entry = quote.bid + distance
                stop_loss = entry + max(distance, abs(entry) * 0.05)
            else:
                action = DecisionAction.LONG
                entry = max(point, quote.ask - distance)
                stop_loss = max(point, entry - max(distance, abs(entry) * 0.05))

            cycle_id = uuid4()
            valid_until = datetime.now(timezone.utc) + timedelta(minutes=5)
            place = Decision(
                cycle_id=cycle_id,
                decision=action,
                symbol=canonical,
                order=OrderInstruction(type=OrderType.LIMIT, entry=entry, size=volume),
                stop_loss=stop_loss,
                risk_percent=0.01,
                valid_until=valid_until,
                reason="DEMO_ONLY_DIAGNOSTIC_PENDING_ORDER",
            )
            placed = await broker.execute(place)
            if placed.status != "PENDING" or not placed.broker_ticket:
                last_error = f"{symbol} diagnostic placement failed: {placed.reason or placed.status}"
                continue

            cancel = Decision(
                cycle_id=cycle_id,
                decision=DecisionAction.CANCEL_ORDER,
                symbol=canonical,
                pending_order_id=placed.broker_ticket,
                valid_until=valid_until,
                reason="DEMO_ONLY_DIAGNOSTIC_CANCEL",
            )
            cancelled = await broker.execute(cancel)
            if cancelled.status != "CANCELLED":
                raise RuntimeError(
                    f"Diagnostic order {placed.broker_ticket} was placed but cancellation failed: "
                    f"{cancelled.reason or cancelled.status}. Cancel it manually in the Demo terminal."
                )
            return {
                "ok": True,
                "canonical": canonical,
                "symbol": symbol,
                "volume": volume,
                "placed_ticket": placed.broker_ticket,
                "placement_status": placed.status,
                "cancel_status": cancelled.status,
            }
        except Exception as exc:
            if "was placed but cancellation failed" in str(exc):
                raise
            last_error = f"{symbol}: {type(exc).__name__}: {exc}"

    raise RuntimeError(last_error)