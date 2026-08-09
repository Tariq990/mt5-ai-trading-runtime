from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import Settings
from .db import Database

logger = logging.getLogger(__name__)

REVIEW_PREFIX = "gpttradder-review"

EVENT_TRADE_CLOSED = "TRADE_CLOSED"
EVENT_TRADE_REJECTED = "TRADE_REJECTED"
EVENT_SYSTEM_ERROR = "SYSTEM_ERROR"
EVENT_PERIODIC_REVIEW = "PERIODIC_REVIEW"
EVENT_DAILY_REVIEW = "DAILY_REVIEW"
EVENT_TEST = "TEST"

# Compact role charter prepended to every review message. The reviewer is
# analysis/advisory only: it never executes trades and never modifies
# parameters; its replies are stored for the audit record.
REVIEWER_CHARTER = (
    "You are the GPTTRADDER REVIEWER. Analyze the event below and reply with "
    "concise findings and recommendations. Advisory only: never execute trades, "
    "never place orders, never propose parameter changes as commands, and never "
    "ask for or output credentials, cookies, tokens or browser state."
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ReviewService:
    """Post-trade/system review channel (separate ChatGPT session).

    - Stable event ids: ``gpttradder-review:trade:<trade_id>``,
      ``gpttradder-review:rejection:<decision_id>``,
      ``gpttradder-review:period:<bucket_ts>``,
      ``gpttradder-review:daily:<date>``.
    - Idempotent: the DB claim (``claim_review_event``) is the durable
      at-most-once boundary — the same event id can never be delivered twice,
      even across process restarts.
    - Non-blocking by contract: every public ``send_*``/``scan_*`` method
      catches all failures internally and the orchestrator fires them as
      background tasks, so reviewer delivery can never stall the trading cycle.
    - Messages are frozen per event (one canonical serialization + SHA-256),
      mirroring the Fix 4 decision-bridge idempotency contract.
    """

    def __init__(self, settings: Settings, db: Database, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.db = db
        self._transport = transport or getattr(settings, "_bridge_transport", None)
        self._pending_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ utils

    def enabled(self) -> bool:
        return self.settings.review_enabled and bool(self.settings.review_conversation_url)

    def fire(self, coro) -> None:
        """Run a review coroutine in the background; failures never propagate."""
        task = asyncio.create_task(coro)
        self._pending_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._pending_tasks.discard(t)
            if not t.cancelled() and t.exception():
                # send_* methods swallow their own errors; this is a final guard.
                logger.error("Unexpected review task failure", exc_info=t.exception())

        task.add_done_callback(_done)

    async def _post(self, event_id: str, message: str) -> dict | None:
        base = {
            "task": "REVIEW_REQUEST",
            "session_key": self.settings.review_session_key,
            "client_message_id": event_id,
            "message": message,
        }
        base_bytes = json.dumps(base, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sha256 = hashlib.sha256(base_bytes).hexdigest()
        payload_bytes = json.dumps({"message_sha256": sha256, **base}, sort_keys=True, separators=(",", ":")).encode("utf-8")
        last_error: Exception | None = None
        for attempt, delay in enumerate(self.settings.review_retry_delays, start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with httpx.AsyncClient(
                    timeout=self.settings.review_timeout_seconds,
                    transport=self._transport,
                ) as client:
                    response = await client.post(
                        self.settings.review_bridge_url,
                        content=payload_bytes,
                        headers={"content-type": "application/json"},
                    )
                    response.raise_for_status()
                    data = response.json()
                    if not data.get("ok"):
                        raise RuntimeError(data.get("error") or "review bridge reported failure")
                    return data
            except Exception as exc:  # noqa: BLE001 — reviewed channel must never raise
                last_error = exc
                logger.warning("Review send attempt %s/%s failed for %s: %s", attempt, len(self.settings.review_retry_delays), event_id, exc)
        raise RuntimeError(f"Review bridge failed after retries: {last_error}")

    async def _dispatch(self, event_id: str, event_type: str, payload: dict, message: str) -> dict | None:
        """Claim (idempotent) then deliver. Never raises."""
        if not self.enabled():
            logger.debug("Review channel disabled; skipping %s", event_id)
            return None
        if not self.db.claim_review_event(event_id, event_type, payload):
            logger.info("Review event already claimed (duplicate suppressed): %s", event_id)
            return None
        try:
            data = await self._post(event_id, message)
            self.db.finish_review_event(event_id, status="SENT", response=(data or {}).get("response"))
            return data
        except Exception as exc:  # noqa: BLE001
            logger.error("Review event %s delivery failed: %s", event_id, exc)
            try:
                self.db.finish_review_event(event_id, status="FAILED")
            except Exception:
                logger.exception("Failed to record review failure for %s", event_id)
            return None

    # -------------------------------------------------------------- events

    async def send_trade_closed(self, record: dict) -> dict | None:
        event_id = f"{REVIEW_PREFIX}:trade:{record.get('trade_id') or record.get('position_id')}"
        message = self._trade_closed_message(record)
        return await self._dispatch(event_id, EVENT_TRADE_CLOSED, record, message)

    async def send_trade_rejected(self, record: dict) -> dict | None:
        event_id = f"{REVIEW_PREFIX}:rejection:{record.get('decision_id')}"
        message = self._trade_rejected_message(record)
        return await self._dispatch(event_id, EVENT_TRADE_REJECTED, record, message)

    async def send_system_error(self, code: str, detail: str, *, now: float | None = None) -> dict | None:
        """Aggregate noisy repeated errors: one send per aggregation window,
        plus one aggregated summary every ``aggregate_every`` occurrences."""
        now = now if now is not None else time.time()
        window = int(self.settings.review_error_aggregation_window_seconds)
        bucket = int(now) // window
        state_key = f"review_error:{code}"
        state = self.db.get_state(state_key, {}) or {}
        if state.get("window") == bucket:
            count = int(state.get("count", 0)) + 1
            self.db.set_state(state_key, {"window": bucket, "count": count, "first_ts": state.get("first_ts", now), "last_ts": now})
            if count % int(self.settings.review_error_aggregate_every) != 0:
                return None
            event_id = f"{REVIEW_PREFIX}:error:{code}:{bucket}:{count}"
            message = self._system_error_message(code, detail, count=count, first_ts=state.get("first_ts", now), last_ts=now)
            return await self._dispatch(event_id, EVENT_SYSTEM_ERROR, {"code": code, "count": count}, message)
        self.db.set_state(state_key, {"window": bucket, "count": 1, "first_ts": now, "last_ts": now})
        event_id = f"{REVIEW_PREFIX}:error:{code}:{bucket}:1"
        message = self._system_error_message(code, detail, count=1, first_ts=now, last_ts=now)
        return await self._dispatch(event_id, EVENT_SYSTEM_ERROR, {"code": code, "count": 1}, message)

    async def maybe_periodic(self, now: datetime | None = None) -> dict | None:
        now = now or utc_now()
        hours = int(self.settings.review_periodic_hours)
        bucket = int(now.timestamp()) // (hours * 3600)
        event_id = f"{REVIEW_PREFIX}:period:{bucket}"
        since = datetime.fromtimestamp(bucket * hours * 3600, tz=timezone.utc).isoformat()
        summary = self.db.get_since_summary(since, self.settings.risk_timezone)
        payload = {"bucket": bucket, "since": since, "summary": summary}
        message = self._periodic_message(summary, since)
        return await self._dispatch(event_id, EVENT_PERIODIC_REVIEW, payload, message)

    async def maybe_daily(self, now: datetime | None = None) -> dict | None:
        now = now or utc_now()
        from zoneinfo import ZoneInfo

        local = now.astimezone(ZoneInfo(self.settings.risk_timezone))
        if (local.hour, local.minute) < (self.settings.review_daily_hour, self.settings.review_daily_minute):
            return None
        day = local.date().isoformat()
        event_id = f"{REVIEW_PREFIX}:daily:{day}"
        if self.db.review_event_exists(event_id):
            return None
        summary = self.db.get_day_summary(day, self.settings.risk_timezone)
        closed_trades = self.db.get_review_payloads(EVENT_TRADE_CLOSED)
        rejections = self.db.get_review_payloads(EVENT_TRADE_REJECTED)
        payload = {"day": day, "summary": summary}
        message = self._daily_message(day, summary, closed_trades, rejections)
        return await self._dispatch(event_id, EVENT_DAILY_REVIEW, payload, message)

    async def send_test_event(self) -> dict | None:
        event_id = f"{REVIEW_PREFIX}:test:{int(time.time() * 1000)}"
        message = (
            f"{REVIEWER_CHARTER}\n\n"
            "REVIEW_EVENT: TEST\n"
            "This is a connectivity/contract test event from the GPTTRADDER runtime. "
            "Reply with OK plus a one-line summary of what event types you expect to receive."
        )
        return await self._dispatch(event_id, EVENT_TEST, {"kind": "test"}, message)

    async def process_pending(self, now: datetime | None = None) -> None:
        """Background review loop tick: fires periodic and daily summaries.

        Never raises — the runtime review loop must not die on reviewer
        channel problems.
        """
        try:
            await self.maybe_periodic(now)
        except Exception:  # noqa: BLE001
            logger.exception("Periodic review failed")
        try:
            await self.maybe_daily(now)
        except Exception:  # noqa: BLE001
            logger.exception("Daily review failed")

    # ------------------------------------------------- position-close scanner

    async def scan_closed_trades(self, packet: Any, *, now: datetime | None = None) -> list[dict]:
        """Diff the durable position snapshot against the current packet and
        emit one TRADE_CLOSED review event per vanished position.

        The snapshot lives in DB state so restarts never lose a close.
        """
        now = now or utc_now()
        snapshot = self.db.get_state("review:positions_snapshot", {}) or {}
        current = {p.position_id: p for p in packet.positions}
        closed: list[dict] = []
        for position_id, entry in snapshot.items():
            if position_id in current:
                continue
            record = dict(entry)
            record["position_id"] = position_id
            record["trade_id"] = position_id
            record["closed_at"] = now.isoformat()
            self._enrich_close(record, packet)
            closed.append(record)
            await self.send_trade_closed(record)
        fresh = {pid: self._snapshot_entry(pos) for pid, pos in current.items()}
        self.db.set_state("review:positions_snapshot", fresh)
        return closed

    @staticmethod
    def _snapshot_entry(position: Any) -> dict:
        return {
            "symbol": position.symbol,
            "side": position.side.value if hasattr(position.side, "value") else str(position.side),
            "size": position.size,
            "entry_price": position.entry_price,
            "stop_loss": position.stop_loss,
            "take_profit": position.take_profit,
            "opened_at": position.opened_at.isoformat() if position.opened_at else None,
            "open_pnl": position.unrealized_pnl,
            "last_price": position.current_price,
        }

    def _enrich_close(self, record: dict, packet: Any) -> None:
        """Attach broker statement truth (deals) when available."""
        deals = [t for t in getattr(packet, "recent_trades", []) or [] if str(t.get("position_id")) == str(record["position_id"])]
        if deals:
            deal = deals[-1]
            record["broker_deal"] = {
                "ticket": deal.get("ticket"),
                "price": deal.get("price"),
                "profit": deal.get("profit") or deal.get("pnl"),
                "comment": deal.get("comment"),
                "volume": deal.get("volume"),
            }
        if record.get("broker_deal", {}).get("price") is not None:
            record["close_price"] = record["broker_deal"]["price"]
            record["realized_pnl"] = record["broker_deal"].get("profit")
        elif record.get("close_price") is None:
            record["close_price"] = record.get("last_price")
        record["close_reason"] = record.get("close_reason") or (record.get("broker_deal", {}).get("comment") or "position closed at broker")

    # ----------------------------------------------------------- messages

    def _header(self, event_label: str) -> list[str]:
        return [REVIEWER_CHARTER, "", f"REVIEW_EVENT: {event_label}"]

    @staticmethod
    def _fmt_ts(value) -> str:
        if not value:
            return "-"
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%SZ")
        except Exception:
            return str(value)

    def _trade_closed_message(self, r: dict) -> str:
        entry = r.get("entry_price")
        close = r.get("close_price")
        sl = r.get("stop_loss")
        realized = r.get("realized_pnl")
        r_multiple = None
        if entry and close and sl and float(close) != float(sl):
            risk = abs(float(entry) - float(sl))
            if risk > 0:
                direction = 1 if r.get("side") == "LONG" else -1
                r_multiple = (float(close) - float(entry)) * direction / risk
        lines = self._header(EVENT_TRADE_CLOSED)
        lines += [
            f"Symbol: {r.get('symbol')}",
            f"Trade ID: {r.get('trade_id')}",
            f"Side: {r.get('side')} | Size: {r.get('size')}",
            f"Entry: {entry} | Close: {close}",
            f"SL: {sl} | TP: {r.get('take_profit')}",
            f"Opened: {self._fmt_ts(r.get('opened_at'))} | Closed: {self._fmt_ts(r.get('closed_at'))}",
            f"Realized PnL: {realized if realized is not None else 'n/a'}",
            f"R multiple: {round(r_multiple, 2) if r_multiple is not None else 'n/a'}",
            f"Fees: n/a (demo)",
            f"Close reason: {r.get('close_reason')}",
        ]
        deal = r.get("broker_deal")
        if deal:
            lines.append(
                f"Minifacts (broker statement): ticket={deal.get('ticket')} price={deal.get('price')} "
                f"profit={deal.get('profit')} comment={deal.get('comment')}"
            )
        if r.get("entry_decision_id"):
            lines.append(f"Entry decision: {r['entry_decision_id']}")
        return "\n".join(lines)

    def _trade_rejected_message(self, r: dict) -> str:
        lines = self._header(EVENT_TRADE_REJECTED)
        lines += [
            f"Symbol: {r.get('symbol')}",
            f"Decision ID: {r.get('decision_id')}",
            f"Decision type: {r.get('decision')}",
            f"Rejected by: {r.get('gate_code')} — {r.get('reason')}",
        ]
        quote = r.get("quote") or {}
        if quote:
            lines.append(f"Quote at rejection: bid={quote.get('bid')} ask={quote.get('ask')} ts={self._fmt_ts(quote.get('ts'))}")
        contract = r.get("contract") or {}
        if contract:
            lines.append(
                f"Contract: vol {contract.get('volume_min')}-{contract.get('volume_max')} step {contract.get('volume_step')} | "
                f"tick_size {contract.get('trade_tick_size')} tick_value {contract.get('trade_tick_value')} "
                f"contract_size {contract.get('trade_contract_size')}"
            )
        risk = r.get("risk_breakdown") or {}
        if risk:
            lines.append(
                f"Monetary risk: SL risk {risk.get('risk_amount')} | margin {risk.get('estimated_margin')} | "
                f"volume {risk.get('requested_volume')}->{risk.get('normalized_volume')} drift {risk.get('normalization_drift_pct')}%"
            )
        decision_json = r.get("decision_json")
        if decision_json:
            lines.append(f"Decision JSON: {decision_json}")
        return "\n".join(lines)

    def _system_error_message(self, code: str, detail: str, *, count: int, first_ts: float, last_ts: float) -> str:
        lines = self._header(EVENT_SYSTEM_ERROR)
        lines += [
            f"Error code: {code}",
            f"Occurrences: {count}",
            f"First: {datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} | "
            f"Last: {datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}",
            f"Detail: {detail[:500]}",
        ]
        return "\n".join(lines)

    def _periodic_message(self, summary: dict, since: str) -> str:
        counts = summary.get("decision_counts") or {}
        execution = summary.get("execution_counts") or {}
        symbols = summary.get("symbol_counts") or {}
        lines = self._header(EVENT_PERIODIC_REVIEW)
        lines += [
            f"Window since: {since}",
            f"Cycles: {summary.get('cycles')} | Decisions: {summary.get('decisions')}",
            f"LONG: {counts.get('LONG', 0)} | SHORT: {counts.get('SHORT', 0)} | WAIT: {counts.get('WAIT', 0)}",
            f"Filled: {execution.get('FILLED', 0)} | Rejected: {execution.get('REJECTED', 0)} | Pending: {execution.get('PENDING', 0)}",
            "Per symbol: " + (" | ".join(f"{k}: {v}" for k, v in sorted(symbols.items())) or "none"),
            f"Equity: {summary.get('start_equity')} -> {summary.get('end_equity')}",
            f"Daily PnL: {summary.get('latest_daily_pnl')} | DD: {summary.get('latest_drawdown_pct')}% | Peak: {summary.get('peak_equity')}",
            f"Open positions: {summary.get('open_positions')} | Pending orders: {summary.get('pending_orders')}",
        ]
        if summary.get("top_rejections"):
            lines.append("Top rejections:")
            lines.extend(f"- {reason}: {count}" for reason, count in summary["top_rejections"][:3])
        return "\n".join(lines)

    def _daily_message(self, day: str, summary: dict, closed_trades: list[dict], rejections: list[dict]) -> str:
        counts = summary.get("decision_counts") or {}
        execution = summary.get("execution_counts") or {}
        lines = self._header(EVENT_DAILY_REVIEW)
        lines += [
            f"Date: {day}",
            f"Cycles: {summary.get('cycles')} | Decisions: {summary.get('decisions')}",
            f"LONG: {counts.get('LONG', 0)} | SHORT: {counts.get('SHORT', 0)} | WAIT: {counts.get('WAIT', 0)}",
            f"Filled: {execution.get('FILLED', 0)} | Rejected: {execution.get('REJECTED', 0)} | Closed: {execution.get('CLOSED', 0)}",
            f"Equity: {summary.get('start_equity')} -> {summary.get('end_equity')}",
            f"Daily PnL: {summary.get('latest_daily_pnl')} | DD: {summary.get('latest_drawdown_pct')}% | Peak: {summary.get('peak_equity')}",
            f"Open positions: {summary.get('open_positions')} | Pending orders: {summary.get('pending_orders')}",
        ]
        if closed_trades:
            lines.append("Closed trades:")
            for t in closed_trades[-10:]:
                lines.append(
                    f"- {t.get('symbol')} {t.get('side')} size={t.get('size')} entry={t.get('entry_price')} "
                    f"close={t.get('close_price')} pnl={t.get('realized_pnl')} reason={t.get('close_reason')}"
                )
        if rejections:
            lines.append(f"Rejected entries: {len(rejections)}")
            for t in rejections[-5:]:
                lines.append(f"- {t.get('symbol')} {t.get('gate_code')}: {t.get('reason')}")
        lines.append(
            "Deep review requested. You may recommend changes to prompt, risk, thresholds, "
            "frequency, symbols or strategy, but you MUST NOT apply any change yourself."
        )
        return "\n".join(lines)
