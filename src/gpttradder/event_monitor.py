from __future__ import annotations

import asyncio
import logging
import time

from .broker.base import Broker
from .config import Settings
from .orchestrator import TradingOrchestrator
from .timeutil import market_session_open

logger = logging.getLogger(__name__)


class MarketEventMonitor:
    """Lightweight broker-side interrupt detector.

    It never makes a trade decision. It only asks the orchestrator for an early
    ChatGPT evaluation when market conditions change materially between 5m scans.
    """

    def __init__(self, broker: Broker, orchestrator: TradingOrchestrator, settings: Settings):
        self.broker = broker
        self.orchestrator = orchestrator
        self.settings = settings
        self._last_mid: dict[str, float] = {}
        self._spread_ema: dict[str, float] = {}
        self._last_trigger: dict[tuple[str, str], float] = {}

    async def run(self) -> None:
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Event monitor scan failed")
            await asyncio.sleep(self.settings.event_poll_seconds)

    async def scan_once(self) -> list[str]:
        # Sample equity frequently so the 10% trailing peak survives fast moves,
        # not merely the five-minute decision cadence.
        self.orchestrator.db.apply_risk_state(
            await self.broker.get_account_state(),
            self.settings.risk_timezone,
        )
        resolution = await self.broker.resolve_symbols(self.settings.symbols, self.settings.symbol_map)
        broker_to_canonical = self.broker.broker_symbol_to_canonical()
        positions = await self.broker.get_positions()
        open_symbols = {broker_to_canonical.get(p.symbol, p.symbol) for p in positions}
        triggered: list[str] = []
        for canonical, res in resolution.items():
            if res.status != "OK" or not res.broker_symbol:
                logger.debug("Skipping unresolved symbol %s (%s)", canonical, res.status)
                continue
            broker_symbol = res.broker_symbol
            quote = await self.broker.get_quote(broker_symbol)
            from datetime import datetime, timezone
            age = (datetime.now(timezone.utc) - quote.ts.astimezone(timezone.utc)).total_seconds()
            if age > self.settings.packet_max_age_seconds or age < -self.settings.quote_future_skew_tolerance_seconds:
                logger.debug("Skipping stale/future event quote for %s: %.1fs", canonical, age)
                continue
            session_open, _ = market_session_open(
                canonical,
                weekend_start_hour_utc=self.settings.market_session_weekend_start_hour_utc,
                weekend_end_hour_utc=self.settings.market_session_weekend_end_hour_utc,
            )
            if not session_open:
                logger.debug("Skipping %s: market session closed", canonical)
                continue
            mid = (quote.bid + quote.ask) / 2.0
            previous = self._last_mid.get(canonical)
            baseline_spread = self._spread_ema.get(canonical, quote.spread)
            # Higher sensitivity while a position is open; ChatGPT can still decide WAIT.
            move_threshold = self.settings.event_move_bps * (0.5 if canonical in open_symbols else 1.0)
            if previous and previous > 0:
                move_bps = abs(mid - previous) / previous * 10_000
                if move_bps >= move_threshold and self._debounced(canonical, "PRICE_MOVE"):
                    triggered.append(f"PRICE_MOVE:{canonical}:{move_bps:.1f}bps")
            if baseline_spread > 0 and quote.spread >= baseline_spread * self.settings.event_spread_multiplier:
                if self._debounced(canonical, "SPREAD_EXPANSION"):
                    triggered.append(f"SPREAD_EXPANSION:{canonical}")

            self._last_mid[canonical] = mid
            self._spread_ema[canonical] = baseline_spread * 0.9 + quote.spread * 0.1

            # Detect a break of the previous completed 5m range.
            try:
                market = await self.broker.get_market_data(broker_symbol, ["5m"], 22)
                candles = market.candles.get("5m", [])
                completed = candles[:-1] if len(candles) > 1 else candles
                reference = completed[-20:]
                if reference:
                    high = max(c.high for c in reference)
                    low = min(c.low for c in reference)
                    if quote.ask > high and self._debounced(canonical, "BREAKOUT_UP"):
                        triggered.append(f"BREAKOUT_UP:{canonical}:{high}")
                    elif quote.bid < low and self._debounced(canonical, "BREAKOUT_DOWN"):
                        triggered.append(f"BREAKOUT_DOWN:{canonical}:{low}")
            except Exception:
                logger.debug("Breakout detector unavailable for %s", canonical, exc_info=True)

        for reason in triggered:
            await self.orchestrator.run_cycle(trigger="EVENT", reason=reason)
        return triggered

    def _debounced(self, symbol: str, reason: str) -> bool:
        key = (symbol, reason)
        now = time.monotonic()
        previous = self._last_trigger.get(key, 0.0)
        if now - previous < self.settings.event_debounce_seconds:
            return False
        self._last_trigger[key] = now
        return True
