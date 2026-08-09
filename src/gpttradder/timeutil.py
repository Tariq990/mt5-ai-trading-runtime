from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

from .symbols import exposure_group_of

# Exposure groups whose market session follows the classic FX calendar (closed
# from Friday evening until Sunday evening UTC). Crypto never closes.
FX_CALENDAR_GROUPS = frozenset({"usd_fx", "metal"})

# Broker clock auto-detection (see measure_broker_utc_offset_hours).
BROKER_OFFSET_MIN_HOURS = -14.0
BROKER_OFFSET_MAX_HOURS = 14.0
BROKER_OFFSET_GRID_HOURS = 0.25
# A quote older than this cannot anchor an offset measurement (weekend-replayed
# quotes would skew the result); such probes are discarded.
BROKER_OFFSET_MAX_QUOTE_AGE_HOURS = 0.5
# Candidate symbols probed at connect time, before canonical resolution exists
# (liquid FX names that every retail MT5 server carries when sessions are open).
OFFSET_PROBE_SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "EURJPY", "USDCAD", "AUDUSD", "USDCHF", "EURGBP")


def normalize_broker_epoch(epoch_seconds: float, server_utc_offset_hours: float) -> datetime:
    """Convert a broker-server timestamp (MT5 clock, usually EET/EEST) to UTC.

    MT5 ticks/candles/deals are stamped in the broker server clock, not UTC.
    Most retail servers sit at UTC+2 (EET) or UTC+3 (EEST). Treating them as
    UTC directly pushes quotes hours into the future, which would mask stale
    symbols as perpetually fresh. This conversion is the canonical fix; the
    freshness gates additionally fail closed on any residual future skew.
    """
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc) - timedelta(hours=server_utc_offset_hours)


def quote_age_seconds(quote_ts: datetime, now: datetime | None = None) -> float:
    """Age of a quote in seconds (negative = timestamp is in the future)."""
    now = now or datetime.now(timezone.utc)
    return (now - quote_ts.astimezone(timezone.utc)).total_seconds()


def quote_is_fresh(
    quote_ts: datetime,
    max_age_seconds: float,
    future_skew_tolerance_seconds: float,
    now: datetime | None = None,
) -> bool:
    """Freshness test, fail closed on future timestamps.

    A quote stamped in the future (broker offset misconfiguration or a broken
    server clock) is NEVER fresh: negative age is as invalid as a too-old age.
    """
    age = quote_age_seconds(quote_ts, now=now)
    return -future_skew_tolerance_seconds <= age <= max_age_seconds + future_skew_tolerance_seconds


def market_session_open(
    symbol: str,
    now: datetime | None = None,
    weekend_start_hour_utc: int = 21,
    weekend_end_hour_utc: int = 21,
) -> tuple[bool, str | None]:
    """Calendar session check per exposure group (fail closed).

    - crypto: 24/7, always open.
    - usd_fx (EURUSD/GBPUSD) and metal (XAU): classic FX weekend closure from
      Friday `weekend_start_hour_utc` until Sunday `weekend_end_hour_utc` UTC.
    - any unclassified symbol: falls back to the FX calendar, conservatively
      assuming the symbol may be closed like a conventional CFD. Holiday
      closures are expressed by the broker trade mode (CLOSEONLY/DISABLED),
      which the safety engine enforces separately.
    """
    now = now or datetime.now(timezone.utc)
    group = exposure_group_of(symbol)
    if group == "crypto":
        return True, None
    now_utc = now.astimezone(timezone.utc)
    friday_close = now_utc - timedelta(days=(now_utc.weekday() - 4) % 7)
    friday_close = friday_close.replace(hour=weekend_start_hour_utc, minute=0, second=0, microsecond=0)
    sunday_open = friday_close + timedelta(days=2)
    sunday_open = sunday_open.replace(hour=weekend_end_hour_utc, minute=0, second=0, microsecond=0)
    if friday_close <= now_utc < sunday_open:
        reason = f"market session closed (weekend: {now_utc:%a %H:%M} UTC)"
        if group is None:
            reason += "; unclassified symbol assumed to follow the FX calendar"
        return False, reason
    return True, None


def broker_utc_offset_plausible(offset_hours: float) -> bool:
    """A broker clock offset is only plausible inside the retail MT5 band."""
    return BROKER_OFFSET_MIN_HOURS <= offset_hours <= BROKER_OFFSET_MAX_HOURS


def round_broker_offset_hours(offset_hours: float) -> float:
    """Quantize to the 15-minute grid real broker clocks use (2.0, 3.0, 4.5...)."""
    return round(offset_hours / BROKER_OFFSET_GRID_HOURS) * BROKER_OFFSET_GRID_HOURS


def measure_broker_utc_offset_hours(
    tick_probe: Callable[[str], float | None],
    candidates: Sequence[str] = OFFSET_PROBE_SYMBOLS,
    now_fn: Callable[[], float] = time.time,
) -> float | None:
    """Auto-detect the broker server clock offset by probing live ticks.

    An MT5 tick is stamped in the server clock: ``tick_msc / 1000`` equals the
    server's wall clock as if it were a Unix epoch. For a fresh quote,
    ``tick_epoch - utc_now`` therefore equals the server's offset (UTC+3 for
    EEST). Only probes whose quotes are near-fresh are usable; the freshest one
    (largest delta) anchors the result, quantized to the 15-minute grid. Returns
    None when no usable probe exists (e.g. full weekend closure) — callers fail
    closed.
    """
    best: float | None = None
    for symbol in candidates:
        epoch = tick_probe(symbol)
        if epoch is None:
            continue
        delta_hours = (epoch - now_fn()) / 3600.0
        # Negative delta = quote stamped "in the future" relative to UTC only
        # when the offset itself is negative (unheard of for MT5); also accept
        # a small margin for clock jitter. Quotes older than the freshness cap
        # carry no offset signal and are discarded.
        if delta_hours < BROKER_OFFSET_GRID_HOURS / -2.0:
            continue
        if -delta_hours > BROKER_OFFSET_MAX_QUOTE_AGE_HOURS:
            continue
        if best is None or delta_hours > best:
            best = delta_hours
    if best is None:
        return None
    rounded = round_broker_offset_hours(best)
    if not broker_utc_offset_plausible(rounded):
        return None
    return rounded