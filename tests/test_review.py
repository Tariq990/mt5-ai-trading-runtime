import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from gpttradder.config import Settings
from gpttradder.db import Database
from gpttradder.models import AccountState, MarketPacket, Position, Side
from gpttradder.review import (
    EVENT_TRADE_CLOSED,
    EVENT_TRADE_REJECTED,
    REVIEW_PREFIX,
    ReviewService,
)


def _settings(**overrides) -> Settings:
    defaults = dict(
        review_enabled=True,
        review_conversation_url="https://chatgpt.com/c/REVIEW-CONV",
        review_bridge_url="http://127.0.0.1:1/review",
        review_session_key="gpttradder-review",
        review_retry_delays=[0, 1],
        review_periodic_hours=6,
        review_daily_hour=22,
        review_daily_minute=0,
        review_error_aggregation_window_seconds=3600,
        review_error_aggregate_every=5,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _db(tmp_path) -> Database:
    return Database(tmp_path / "review.sqlite3")


def _service(settings: Settings, db: Database, transport: httpx.AsyncBaseTransport) -> ReviewService:
    service = ReviewService(settings, db)
    service._transport = transport
    return service


def _position(position_id: str = "p1", symbol: str = "BTC") -> Position:
    return Position(
        position_id=position_id,
        symbol=symbol,
        side=Side.LONG,
        size=0.1,
        entry_price=60000,
        current_price=62000,
        stop_loss=59000,
        take_profit=65000,
        unrealized_pnl=200,
        opened_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )


def _packet(*positions: Position) -> MarketPacket:
    account = AccountState(
        balance=10000,
        equity=10050,
        free_margin=10050,
        daily_pnl=50,
        daily_start_equity=10000,
        peak_equity=10050,
        trailing_drawdown_pct=0,
    )
    return MarketPacket(
        broker_timestamp=datetime.now(timezone.utc).isoformat(),
        account=account,
        symbols={},
        positions=list(positions),
    )


def _ok_handler(calls: list[dict], response_text: str = "Looks fine") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True, "send_confirmed": True, "response": response_text})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_disabled_channel_never_hits_the_bridge(tmp_path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    settings = _settings(review_enabled=False)
    service = _service(settings, _db(tmp_path), httpx.MockTransport(handler))
    result = await service.send_system_error("X", "detail")
    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_trade_rejected_message_is_frozen_and_delivered_once(tmp_path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True, "send_confirmed": True, "response": "ok"})

    settings = _settings()
    db = _db(tmp_path)
    service = _service(settings, db, httpx.MockTransport(handler))
    record = {
        "symbol": "BTC",
        "decision_id": str(uuid.uuid4()),
        "decision": "LONG",
        "gate_code": "DAILY_LOSS_LOCK",
        "reason": "DAILY_LOSS_LOCK: daily loss limit reached",
        "decision_json": "{}",
        "quote": {"bid": 60000, "ask": 60010, "ts": datetime.now(timezone.utc).isoformat()},
        "contract": {"volume_min": 0.01, "volume_max": 100, "volume_step": 0.01},
    }
    first = await service.send_trade_rejected(record)
    assert first is not None
    second = await service.send_trade_rejected(record)
    assert second is None  # idempotent: same event id never delivered twice

    assert len(calls) == 1
    body = calls[0]
    assert body["session_key"] == "gpttradder-review"
    assert body["client_message_id"].startswith(f"{REVIEW_PREFIX}:rejection:")
    assert body["message_sha256"]
    assert "DAILY_LOSS_LOCK" in body["message"]
    assert "reviewer" in body["message"].lower()

    rows = db.get_review_events()
    assert len(rows) == 1
    assert rows[0]["status"] == "SENT"
    assert rows[0]["event_type"] == EVENT_TRADE_REJECTED


@pytest.mark.asyncio
async def test_system_error_aggregation_sends_first_and_nth(tmp_path) -> None:
    calls = []
    settings = _settings(review_error_aggregation_window_seconds=3600, review_error_aggregate_every=3)
    service = _service(settings, _db(tmp_path), _ok_handler(calls))
    t0 = 1_700_000_000.0

    r1 = await service.send_system_error("STALE_PACKET", "first", now=t0)
    r2 = await service.send_system_error("STALE_PACKET", "second", now=t0 + 1)
    r3 = await service.send_system_error("STALE_PACKET", "third", now=t0 + 2)
    assert r1 is not None
    assert r2 is None
    assert r3 is not None
    assert len(calls) == 2
    assert "Occurrences: 3" in calls[1]["message"]


@pytest.mark.asyncio
async def test_error_aggregation_never_skips_a_fresh_window(tmp_path) -> None:
    calls = []
    settings = _settings(review_error_aggregation_window_seconds=60, review_error_aggregate_every=2)
    service = _service(settings, _db(tmp_path), _ok_handler(calls))
    t0 = 1_700_000_000.0

    await service.send_system_error("X", "a", now=t0)
    await service.send_system_error("X", "b", now=t0 + 120)  # new window: first always sends
    await service.send_system_error("X", "c", now=t0 + 121)  # new window: 2nd of window (2 % 2 == 0) also sends
    assert len(calls) == 3
    assert calls[1]["message"].startswith("You are the GPTTRADDER REVIEWER.")
    assert "Occurrences: 1" in calls[1]["message"]


@pytest.mark.asyncio
async def test_scan_closed_trades_emits_close_and_persists_snapshot(tmp_path) -> None:
    calls = []
    settings = _settings()
    db = _db(tmp_path)
    service = _service(settings, db, _ok_handler(calls))

    # First scan: both positions are open; nothing closes.
    first = await service.scan_closed_trades(_packet(_position("p1"), _position("p2")))
    assert first == []

    # Second scan: p1 vanished -> TRADE_CLOSED event; p2 still open.
    second = await service.scan_closed_trades(_packet(_position("p2")))
    assert len(second) == 1
    assert second[0]["position_id"] == "p1"

    assert len(calls) == 1
    body = calls[0]
    assert body["client_message_id"] == f"{REVIEW_PREFIX}:trade:p1"
    assert "BTC" in body["message"]

    # Third scan: p2 vanishes now too.
    third = await service.scan_closed_trades(_packet())
    assert [c["position_id"] for c in third] == ["p2"]

    # No new event for p1: idempotent across restarts (snapshot was durable).
    assert len(db.get_review_events()) == 2


@pytest.mark.asyncio
async def test_maybe_periodic_sends_once_per_bucket(tmp_path) -> None:
    calls = []
    settings = _settings(review_periodic_hours=6)
    db = _db(tmp_path)
    service = _service(settings, db, _ok_handler(calls))
    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

    first = await service.maybe_periodic(now)
    second = await service.maybe_periodic(now + timedelta(minutes=5))
    assert first is not None
    assert second is None
    assert len(calls) == 1
    assert calls[0]["client_message_id"].startswith(f"{REVIEW_PREFIX}:period:")
    assert "Cycles" in calls[0]["message"]


@pytest.mark.asyncio
async def test_maybe_daily_sends_once_per_local_day_after_threshold(tmp_path) -> None:
    calls = []
    settings = _settings(review_daily_hour=22, review_daily_minute=0, risk_timezone="UTC")
    db = _db(tmp_path)
    service = _service(settings, db, _ok_handler(calls))

    # Before the threshold: no send.
    before = datetime(2026, 8, 10, 21, 59, tzinfo=timezone.utc)
    assert await service.maybe_daily(before) is None

    # After the threshold: one send per local day.
    after = datetime(2026, 8, 10, 22, 5, tzinfo=timezone.utc)
    first = await service.maybe_daily(after)
    assert first is not None
    assert await service.maybe_daily(after + timedelta(minutes=10)) is None

    # Next local day fires again.
    next_day = datetime(2026, 8, 11, 22, 5, tzinfo=timezone.utc)
    assert await service.maybe_daily(next_day) is not None
    assert len(calls) == 2
    assert "Date: 2026-08-10" in calls[0]["message"]


@pytest.mark.asyncio
async def test_bridge_failure_is_recorded_and_never_raises(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": "review bridge exploded"})

    settings = _settings(review_retry_delays=[0, 0])
    db = _db(tmp_path)
    service = _service(settings, db, httpx.MockTransport(handler))
    result = await service.send_system_error("X", "detail")
    assert result is None
    rows = db.get_review_events()
    assert len(rows) == 1
    assert rows[0]["status"] == "FAILED"


@pytest.mark.asyncio
async def test_process_pending_never_raises(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": "down"})

    settings = _settings(review_retry_delays=[0, 0], risk_timezone="UTC")
    db = _db(tmp_path)
    service = _service(settings, db, httpx.MockTransport(handler))
    now = datetime(2026, 8, 10, 22, 30, tzinfo=timezone.utc)
    await service.process_pending(now)  # must not raise
    rows = db.get_review_events()
    assert any(row["status"] == "FAILED" for row in rows)


@pytest.mark.asyncio
async def test_event_claim_survives_restart_with_same_db(tmp_path) -> None:
    calls = []
    settings = _settings()
    db_path = tmp_path / "shared.sqlite3"

    db1 = Database(db_path)
    s1 = _service(settings, db1, _ok_handler(calls))
    await s1.send_trade_rejected({"symbol": "ETH", "decision_id": "d-restart", "decision": "SHORT", "gate_code": "X", "reason": "y"})

    db2 = Database(db_path)  # fresh connection = process restart
    s2 = _service(settings, db2, _ok_handler(calls))
    result = await s2.send_trade_rejected({"symbol": "ETH", "decision_id": "d-restart", "decision": "SHORT", "gate_code": "X", "reason": "y"})
    assert result is None  # duplicate suppressed across restart
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_disabled_error_events_do_not_pollute_db(tmp_path) -> None:
    calls = []
    settings = _settings(review_enabled=False)
    db = _db(tmp_path)
    service = _service(settings, db, _ok_handler(calls))
    await service.send_system_error("X", "d")
    assert db.get_review_events() == []
