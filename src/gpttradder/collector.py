from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Callable

from .broker.base import Broker
from .config import Settings
from .hashing import market_packet_hash
from .models import MarketPacket, SymbolStatus
from .risk import compute_exposure
from .timeutil import market_session_open, quote_age_seconds, quote_is_fresh

TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

# Trade modes that accept NEW entries. CLOSEONLY/DISABLED are never executable.
_ENTRY_TRADE_MODES = frozenset({"FULL", "LONGONLY", "SHORTONLY"})


class MarketCollector:
    def __init__(self, broker: Broker, settings: Settings, now_fn: Callable[[], datetime] | None = None):
        self.broker = broker
        self.settings = settings
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    def _quote_status(self, canonical: str, res, data) -> SymbolStatus:
        """Decompose one symbol's live state into explicit, fail-closed flags."""
        now = self._now()
        quote_age = quote_age_seconds(data.quote.ts, now=now)
        contract = data.contract
        trade_mode = (
            contract.trade_mode_label
            if contract is not None and contract.trade_mode_label
            else str(data.market.get("trade_mode_label", "FULL")).upper()
        )
        contract_blocks = contract is not None and not contract.is_tradable
        session_open, session_reason = market_session_open(
            canonical,
            now=now,
            weekend_start_hour_utc=self.settings.market_session_weekend_start_hour_utc,
            weekend_end_hour_utc=self.settings.market_session_weekend_end_hour_utc,
        )
        quote_fresh = quote_is_fresh(
            data.quote.ts,
            max_age_seconds=self.settings.packet_max_age_seconds,
            future_skew_tolerance_seconds=self.settings.quote_future_skew_tolerance_seconds,
            now=now,
        )
        if contract_blocks:
            status = "NOT_TRADABLE"
            reason = f"trade mode {trade_mode or 'non-tradable'}"
        elif not quote_fresh:
            status = "NOT_TRADABLE"
            reason = f"quote is {max(0.0, quote_age):.0f}s old (no live tick; market likely closed)"
        elif not session_open:
            status = "NOT_TRADABLE"
            reason = session_reason or "market session closed"
        else:
            status = "TRADABLE"
            reason = None
        executable = (
            status == "TRADABLE"
            and quote_fresh
            and session_open
            and trade_mode in _ENTRY_TRADE_MODES
        )
        return SymbolStatus(
            canonical=canonical,
            broker_symbol=res.broker_symbol,
            status=status,  # type: ignore[arg-type]
            reason=reason,
            broker_trade_mode=trade_mode,
            quote_fresh=quote_fresh,
            quote_age_seconds=round(quote_age, 3),
            market_session_open=session_open,
            executable_now=executable,
        )

    async def collect(self, trigger: str = "POLL", reason: str | None = None) -> MarketPacket:
        resolution = await self.broker.resolve_symbols(self.settings.symbols, self.settings.symbol_map)
        broker_to_canonical = self.broker.broker_symbol_to_canonical()
        tradable = {
            canonical: res
            for canonical, res in resolution.items()
            if res.broker_symbol
        }

        account_task = self.broker.get_account_state()
        position_task = self.broker.get_positions()
        orders_task = self.broker.get_pending_orders()
        trades_task = self.broker.get_recent_trades()
        market_tasks = {
            canonical: asyncio.create_task(
                self.broker.get_market_data(res.broker_symbol, TIMEFRAMES, self.settings.candle_limit)
            )
            for canonical, res in tradable.items()
        }
        account, positions, pending_orders, recent_trades = await asyncio.gather(
            account_task, position_task, orders_task, trades_task
        )

        symbol_data: dict = {}
        symbol_status: dict[str, SymbolStatus] = {}
        for canonical, task in market_tasks.items():
            res = tradable[canonical]
            assert res.broker_symbol
            try:
                data = await task
            except Exception as exc:
                symbol_status[canonical] = SymbolStatus(
                    canonical=canonical,
                    broker_symbol=res.broker_symbol,
                    status="NO_QUOTE",
                    reason=f"{type(exc).__name__}: {exc}",
                )
                continue
            # A symbol whose market is closed (zero or inverted quote) is excluded
            # from the packet entirely: ChatGPT must never see a garbage price that
            # could produce an entry, and the safety engine would reject it anyway.
            sane_quote = data.quote.ask > 0 and data.quote.bid > 0 and data.quote.ask >= data.quote.bid
            if not sane_quote:
                symbol_status[canonical] = SymbolStatus(
                    canonical=canonical,
                    broker_symbol=res.broker_symbol,
                    status="NO_QUOTE",
                    reason="market closed or no live quote",
                )
                continue
            # A quote stamped in the future (after broker-time normalization) is
            # a clock/offset failure, NOT a fresh quote. Fail closed: exclude the
            # symbol from the packet entirely.
            quote_age = quote_age_seconds(data.quote.ts, now=self._now())
            if quote_age < -self.settings.quote_future_skew_tolerance_seconds:
                symbol_status[canonical] = SymbolStatus(
                    canonical=canonical,
                    broker_symbol=res.broker_symbol,
                    status="TIME_INVALID",
                    reason=(
                        f"quote timestamp {quote_age:.1f}s in the future after broker UTC "
                        "normalization (broker server clock or GPTTRADDER_BROKER_SERVER_UTC_OFFSET_HOURS misconfigured)"
                    ),
                    quote_fresh=False,
                    quote_age_seconds=round(quote_age, 3),
                )
                continue
            contract = data.contract
            status = self._quote_status(canonical, res, data)
            symbol_status[canonical] = status
            # NOT_TRADABLE symbols stay in the packet so ChatGPT sees that they
            # exist and why; executable_now=false blocks any new entry on them.
            if status.status == "NOT_TRADABLE":
                symbol_data[canonical] = data.model_copy(update={"symbol": canonical})
                continue
            symbol_data[canonical] = data.model_copy(update={"symbol": canonical})
        for canonical, res in resolution.items():
            if canonical in symbol_status:
                continue
            broker_symbol = getattr(res, "broker_symbol", None)
            status: str = "UNRESOLVED"
            if res.status == "AMBIGUOUS":
                status = "AMBIGUOUS"
            symbol_status[canonical] = SymbolStatus(
                canonical=canonical,
                broker_symbol=broker_symbol,
                status=status,  # type: ignore[arg-type]
                reason=res.reason,
                executable_now=False,
            )
        if not symbol_data:
            raise RuntimeError("No configured symbol has a live quote (markets appear closed).")

        # Positions/pending orders/trades carry broker symbol names; translate to
        # canonical so safety gates and ChatGPT share one symbol namespace.
        positions = [
            p.model_copy(update={"symbol": broker_to_canonical.get(p.symbol, p.symbol)})
            for p in positions
        ]
        pending_orders = [
            o.model_copy(update={"symbol": broker_to_canonical.get(o.symbol, o.symbol)})
            for o in pending_orders
        ]
        recent_trades = [
            {
                **row,
                **({"symbol": broker_to_canonical.get(row["symbol"], row["symbol"])} if row.get("symbol") else {}),
            }
            for row in recent_trades
        ]

        contracts = {canonical: data.contract for canonical, data in symbol_data.items()}
        exposure = compute_exposure(positions, contracts)

        broker_ts = max(item.quote.ts for item in symbol_data.values())
        packet = MarketPacket(
            broker_timestamp=broker_ts.astimezone(timezone.utc),
            symbols=symbol_data,
            account=account,
            positions=positions,
            pending_orders=pending_orders,
            recent_trades=recent_trades,
            trigger=trigger,
            trigger_reason=reason,
            exposure=exposure,
            symbol_status=symbol_status,
        )
        packet.packet_hash = market_packet_hash(packet)
        return packet