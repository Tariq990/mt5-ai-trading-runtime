from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from .broker.base import Broker
from .config import Settings
from .db import Database
from .notifications import build_notification_service
from .timeutil import market_session_open, quote_age_seconds


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


async def run_preflight(settings: Settings, broker: Broker, db: Database, check_bridge: bool = True) -> list[Check]:
    checks: list[Check] = []
    try:
        await broker.connect()
        await broker.assert_demo()
        account = await broker.get_account_state()
        checks.append(Check(
            "demo_account",
            account.is_demo,
            f"account={account.account_id}, mode={account.account_mode}, hedging={account.hedging_allowed}",
        ))
    except Exception as exc:
        return [Check("demo_account", False, f"{type(exc).__name__}: {exc}")]

    try:
        durable = db.apply_risk_state(account, settings.risk_timezone)
        checks.append(Check("risk_state", True, f"daily_start={durable.daily_start_equity}, peak={durable.peak_equity}"))
    except Exception as exc:
        checks.append(Check("risk_state", False, f"{type(exc).__name__}: {exc}"))

    resolution: dict = {}
    try:
        resolution = await broker.resolve_symbols(settings.symbols, settings.symbol_map)
        for canonical in settings.symbols:
            res = resolution.get(canonical)
            if res is None:
                checks.append(Check(f"symbol:{canonical}", False, "not evaluated"))
                continue
            if res.status != "OK" or not res.broker_symbol:
                checks.append(
                    Check(f"symbol:{canonical}", False, f"unresolved ({res.status}): {res.reason}")
                )
                continue
            checks.append(Check(f"symbol:{canonical}", True, f"broker_symbol={res.broker_symbol}"))
    except Exception as exc:
        checks.append(Check("symbols_resolved", False, f"{type(exc).__name__}: {exc}"))

    for canonical, res in resolution.items():
        if res.status != "OK" or not res.broker_symbol:
            checks.append(Check(f"market:{canonical}", False, f"unresolved ({res.status}): {res.reason}"))
            continue
        symbol = res.broker_symbol
        try:
            quote = await broker.get_quote(symbol)
            data = await broker.get_market_data(symbol, ["1m", "5m", "1h", "4h", "1d"], 30)
            complete = all(len(data.candles.get(tf, [])) >= 1 for tf in ["1m", "5m", "1h", "4h", "1d"])
            sane_quote = quote.ask > 0 and quote.bid > 0 and quote.ask >= quote.bid
            quote_age = quote_age_seconds(quote.ts)
            time_ok = quote_age >= -settings.quote_future_skew_tolerance_seconds
            session_open, session_reason = market_session_open(
                canonical,
                weekend_start_hour_utc=settings.market_session_weekend_start_hour_utc,
                weekend_end_hour_utc=settings.market_session_weekend_end_hour_utc,
            )
            contract = data.contract
            contract_ok = contract is not None and contract.volume_min is not None and contract.volume_step is not None
            checks.append(
                Check(
                    f"contract:{canonical}",
                    contract_ok,
                    (
                        "" if contract is None else
                        f"tick_size={contract.trade_tick_size}, tick_value={contract.trade_tick_value}, "
                        f"contract_size={contract.trade_contract_size}, vol={contract.volume_min}/{contract.volume_max}/{contract.volume_step}, "
                        f"trade_mode={contract.trade_mode_label}"
                    ),
                )
            )
            detail = f"bid={quote.bid}, ask={quote.ask}, spread={quote.spread}, quote_age={quote_age:.1f}s"
            if not sane_quote:
                detail += " (market closed: no live quote)"
            if not time_ok:
                detail += " (TIME INVALID: quote stamped in the future; check GPTTRADDER_BROKER_SERVER_UTC_OFFSET_HOURS)"
            if session_reason:
                detail += f" ({session_reason})"
            checks.append(
                Check(
                    f"market:{canonical}",
                    complete and sane_quote and time_ok,
                    detail,
                )
            )
        except Exception as exc:
            checks.append(Check(f"market:{canonical}", False, f"{type(exc).__name__}: {exc}"))

    try:
        positions = await broker.get_positions()
        orders = await broker.get_pending_orders()
        await broker.get_recent_trades()
        checks.append(Check("account_inventory", True, f"positions={len(positions)}, pending={len(orders)}"))
    except Exception as exc:
        checks.append(Check("account_inventory", False, f"{type(exc).__name__}: {exc}"))

    if check_bridge:
        try:
            parts = urlsplit(settings.decision_bridge_url)
            health_url = urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(health_url)
                response.raise_for_status()
                payload = response.json()
            checks.append(Check("chatgpt_bridge", bool(payload.get("ok")), f"url={health_url}"))
        except Exception as exc:
            checks.append(Check("chatgpt_bridge", False, f"{type(exc).__name__}: {exc}"))

    if settings.telegram_enabled:
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            checks.append(Check("telegram", False, "enabled but GPTTRADDER_TELEGRAM_BOT_TOKEN / CHAT_ID are missing"))
        else:
            service = build_notification_service(settings, db)
            ok, detail = await service.healthcheck()
            checks.append(Check("telegram", ok, detail))
    else:
        checks.append(Check("telegram", True, "disabled (optional)"))

    return checks
