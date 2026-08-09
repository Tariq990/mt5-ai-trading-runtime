from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .broker.base import Broker
from .config import Settings
from .db import Database
from .notifications import NotificationService


@dataclass(frozen=True)
class DailyReport:
    report_date: str
    text: str
    payload: dict


def build_daily_report(db: Database, settings: Settings, now: datetime | None = None) -> DailyReport:
    tz = ZoneInfo(settings.risk_timezone)
    local_now = now.astimezone(tz) if now else datetime.now(tz)
    day = local_now.date().isoformat()
    summary = db.get_day_summary(day, settings.risk_timezone)
    action_counts = summary["decision_counts"]
    execution_counts = summary["execution_counts"]
    symbols = summary["symbol_counts"]

    lines = [
        f"GPTTRADDER Daily Report — {day}",
        f"Cycles: {summary['cycles']}",
        f"Decisions: {summary['decisions']}",
        f"LONG: {action_counts.get('LONG', 0)} | SHORT: {action_counts.get('SHORT', 0)} | WAIT: {action_counts.get('WAIT', 0)}",
        f"Filled: {execution_counts.get('FILLED', 0)} | Pending: {execution_counts.get('PENDING', 0)} | Rejected: {execution_counts.get('REJECTED', 0)}",
        "Symbols: " + (" | ".join(f"{name}: {count}" for name, count in sorted(symbols.items())) or "none"),
    ]
    if summary["start_equity"] is not None and summary["end_equity"] is not None:
        change = summary["end_equity"] - summary["start_equity"]
        lines.append(
            f"Equity: {summary['start_equity']:.2f} → {summary['end_equity']:.2f} ({change:+.2f})"
        )
    if summary["latest_daily_pnl"] is not None:
        lines.append(f"Daily PnL: {summary['latest_daily_pnl']:+.2f}")
    if summary["latest_drawdown_pct"] is not None:
        lines.append(f"Trailing DD: {summary['latest_drawdown_pct']:.2f}%")
    lines.append(f"Open positions: {summary['open_positions']} | Pending orders: {summary['pending_orders']}")
    packet = db.latest_packet()
    if packet:
        symbols = packet.get("symbols", {})
        statuses = packet.get("symbol_status", {})
        if symbols:
            lines.append("Market state:")
            for sym in sorted(symbols):
                st = (statuses.get(sym) or {}).get("status", "?")
                broker = ((symbols[sym].get("contract") or {}).get("broker_symbol")) or "?"
                lines.append(f"- {sym} ({broker}): {st}")
        exposure = packet.get("exposure") or {}
        if exposure.get("total_gross_exposure") is not None:
            gross = float(exposure["total_gross_exposure"])
            net = float(exposure["total_net_exposure"])
            direction = exposure.get("usd_direction")
            direction_txt = (
                f" ({direction:+.4f} USD direction)" if isinstance(direction, (int, float)) else ""
            )
            lines.append(f"Exposure: gross {gross:.2f} | net {net:.2f}{direction_txt}")
            for group_name, group in sorted((exposure.get("groups") or {}).items()):
                lines.append(
                    f"- {group_name}: gross {group['gross_exposure']:.2f} | "
                    f"net {group['net_exposure']:.2f} | positions {group['position_count']}"
                )
    if summary["top_rejections"]:
        lines.append("Top rejections:")
        lines.extend(f"- {reason}: {count}" for reason, count in summary["top_rejections"][:3])

    return DailyReport(report_date=day, text="\n".join(lines), payload=summary)


def seconds_until_report(settings: Settings, now: datetime | None = None) -> float:
    tz = ZoneInfo(settings.risk_timezone)
    local_now = now.astimezone(tz) if now else datetime.now(tz)
    target = local_now.replace(
        hour=settings.daily_report_hour,
        minute=settings.daily_report_minute,
        second=0,
        microsecond=0,
    )
    if target <= local_now:
        target += timedelta(days=1)
    return max(0.0, (target - local_now).total_seconds())


async def send_daily_report(
    db: Database,
    settings: Settings,
    notifications: NotificationService,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> DailyReport:
    report = build_daily_report(db, settings, now=now)
    stored = db.get_daily_report(report.report_date)
    if not force and stored is not None and bool(stored.get("sent")):
        return report
    sent = await notifications.notifier.send(report.text)
    db.save_daily_report(report.report_date, report.payload, report.text, sent=sent)
    return report


async def daily_report_loop(
    db: Database,
    settings: Settings,
    notifications: NotificationService,
) -> None:
    while True:
        await asyncio.sleep(seconds_until_report(settings))
        while True:
            try:
                report = await send_daily_report(db, settings, notifications)
                stored = db.get_daily_report(report.report_date)
                # If Telegram is disabled, durable report generation is the goal.
                # If it is enabled, retry delivery every five minutes until sent.
                if not settings.telegram_enabled or (stored and bool(stored.get("sent"))):
                    break
            except asyncio.CancelledError:
                raise
            except Exception:
                await notifications.runtime(
                    "Daily report generation/delivery failed.",
                    severity="ERROR",
                    key="daily-report-failure",
                )
            await asyncio.sleep(300)
        await asyncio.sleep(1.0)
