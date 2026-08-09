from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .symbols import exposure_group_of

# Exposure groups whose market session follows the classic FX calendar (closed
# from Friday evening until Sunday evening UTC). Crypto never closes.
FX_CALENDAR_GROUPS = frozenset({"usd_fx", "metal"})


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