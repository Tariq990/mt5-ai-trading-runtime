from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

from gpttradder.config import Settings
from gpttradder.db import Database
from gpttradder.decision.http_bridge import HTTPDecisionBridge
from gpttradder.models import (
    AccountState,
    Decision,
    DecisionAction,
    MarketPacket,
    Quote,
    SymbolContractSpec,
    SymbolMarketData,
)

UTC = timezone.utc


def make_packet(cycle_id=None):
    now = datetime.now(UTC)
    return MarketPacket(
        cycle_id=cycle_id or uuid4(),
        broker_timestamp=now,
        packet_created_at=now,
        account=AccountState(
            account_id="demo", is_demo=True, balance=10_000, equity=10_000, free_margin=10_000,
            account_mode="HEDGING", hedging_allowed=True, daily_pnl=0, daily_start_equity=10_000,
            peak_equity=10_000, trailing_drawdown_pct=0,
        ),
        symbols={
            "BTC": SymbolMarketData(
                symbol="BTC", quote=Quote(bid=65000, ask=65010, spread=10, ts=now),
                candles={},
                contract=SymbolContractSpec(
                    broker_symbol="BTCUSD", canonical_symbol="BTC", is_tradable=True, digits=2,
                    point=0.01, trade_tick_size=0.01, trade_tick_value=0.01, trade_contract_size=1.0,
                    volume_min=0.01, volume_max=100.0, volume_step=0.01, trade_stops_level=0,
                    trade_freeze_level=0, trade_mode=4, trade_mode_label="FULL",
                    currency_base="BTC", currency_profit="USD", currency_margin="USD",
                ),
            )
        },
        positions=[],
    )


def wait_decision(packet) -> Decision:
    return Decision(
        cycle_id=packet.cycle_id,
        decision=DecisionAction.WAIT,
        symbol=None,
        valid_until=datetime.now(UTC) + timedelta(minutes=5),
        reason="test",
    )


def bridge_with_transport(handler, on_send=None) -> HTTPDecisionBridge:
    settings = Settings()
    settings.decision_retry_delays = [0, 0, 0]
    settings._bridge_transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
    return HTTPDecisionBridge(settings, on_send=on_send)


def body_of(request) -> dict:
    return json.loads(request.content)


async def test_a_same_cycle_retries_are_byte_identical_and_single_id():
    packet = make_packet()
    attempts = []
    seen_bodies = set()

    async def handler(request):
        attempts.append(body_of(request))
        seen_bodies.add(bytes(request.content))
        if len(attempts) < 3:
            # transient pre-dispatch failures (proven nothing was sent) are
            # safely retried with the byte-identical frozen payload
            return httpx.Response(
                502, request=request,
                json={
                    "error": f"transient hiccup #{len(attempts)}",
                    "dispatch_state": "FAILED_BEFORE_DISPATCH",
                    "retryable": True,
                },
            )
        return httpx.Response(200, request=request, json=wait_decision(packet).model_dump(mode="json"))

    bridge = bridge_with_transport(handler)
    decision = await bridge.decide(packet)
    assert decision.decision == DecisionAction.WAIT
    assert len(attempts) == 3
    assert len(seen_bodies) == 1, "every retry must reuse the byte-identical frozen payload"
    for body in attempts:
        assert body["market_packet"]["cycle_id"] == str(packet.cycle_id)


async def test_a2_on_send_records_sha_and_attempt_for_every_retry():
    from gpttradder.decision.http_bridge import DecisionBridgeError

    packet = make_packet()
    records = []

    settings = Settings(
        chatgpt_timeout_seconds=15,
        decision_timeout_seconds=16,
        decision_reconcile_interval_seconds=5,
        decision_retry_delays=[0, 0, 0],
    )
    settings._bridge_transport = httpx.MockTransport(lambda request: httpx.Response(
        502, request=request,
        json={
            "error": f"transient #{len(records) + 1}",
            "dispatch_state": "FAILED_BEFORE_DISPATCH",
            "retryable": True,
        },
    ))
    bridge = HTTPDecisionBridge(settings, on_send=lambda **kw: records.append(kw))
    with pytest.raises(DecisionBridgeError):
        await bridge.decide(packet)
    attempts = [r["attempt"] for r in records]
    assert attempts == list(range(1, len(attempts) + 1)), "attempts are consecutive"
    assert len(attempts) >= 3, "pre-dispatch failures are retried, not abandoned after 2"
    shas = {r["message_sha256"] for r in records}
    assert len(shas) == 1, "one frozen sha per cycle, across all attempts"
    ids = {r["client_message_id"] for r in records}
    assert ids == {f"gpttradder:{packet.cycle_id}"}
    states = {r["dispatch_state"] for r in records}
    assert states == {"FAILED_BEFORE_DISPATCH"}


async def test_b_same_cycle_with_altered_content_fails_closed():
    bridge = HTTPDecisionBridge(Settings())
    packet = make_packet()
    bridge.freeze(packet)
    tampered = packet.model_copy(
        update={"symbols": {
            "BTC": packet.symbols["BTC"].model_copy(
                update={"quote": Quote(bid=999, ask=1000, spread=1, ts=packet.broker_timestamp)}
            )
        }}
    )
    with pytest.raises(RuntimeError, match="already frozen with a different message"):
        bridge.freeze(tampered)
    # the original frozen payload stays intact and reusable
    payload, sha, client_id = bridge.freeze(packet)
    assert client_id == f"gpttradder:{packet.cycle_id}"
    assert len(sha) == 64
    assert json.loads(payload)["message_sha256"] == sha


async def test_c_new_cycle_gets_new_id_and_fresh_fingerprint():
    bridge = HTTPDecisionBridge(Settings())
    a = make_packet()
    b = make_packet()
    payload_a, sha_a, id_a = bridge.freeze(a)
    payload_b, sha_b, id_b = bridge.freeze(b)
    assert id_a != id_b
    assert sha_a != sha_b
    assert payload_a != payload_b


async def test_d_cached_response_reuse_returns_stable_decision():
    packet = make_packet()
    responses = []
    sent_bodies = []

    async def handler(request):
        sent_bodies.append(bytes(request.content))
        responses.append(httpx.Response(200, request=request, json=wait_decision(packet).model_dump(mode="json")))
        return responses[-1]

    bridge = bridge_with_transport(handler)
    first = await bridge.decide(packet)
    second = await bridge.decide(packet)  # same cycle re-decided (claimed/duplicate path)
    assert first.decision == second.decision == DecisionAction.WAIT
    assert first.cycle_id == second.cycle_id == packet.cycle_id
    assert len(sent_bodies) == 2
    assert len(set(sent_bodies)) == 1, "cached reuse must not drift the payload"


async def test_e_transport_retries_cannot_duplicate_execution():
    from pathlib import Path
    from tempfile import mkdtemp

    from gpttradder.broker.simulated import SimulatedBroker
    from gpttradder.collector import MarketCollector
    from gpttradder.factory import build_notification_service
    from gpttradder.orchestrator import TradingOrchestrator
    from gpttradder.safety import SafetyEngine

    settings = Settings()
    settings.broker = "simulated"
    settings.decision_retry_delays = [0, 0]
    settings.db_path = Path(mkdtemp()) / "state.sqlite"
    db = Database(settings.db_path)
    attempts = []

    async def handler(request):
        body = body_of(request)
        cycle_id = body["market_packet"]["cycle_id"]
        attempts.append(1)
        if len(attempts) < 2:
            return httpx.Response(
                502, request=request,
                json={"error": "flaky transport", "dispatch_state": "FAILED_BEFORE_DISPATCH", "retryable": True},
            )
        decision = Decision(
            cycle_id=cycle_id,
            decision=DecisionAction.WAIT,
            symbol=None,
            valid_until=datetime.now(UTC) + timedelta(minutes=5),
            reason="test",
        )
        return httpx.Response(200, request=request, json=decision.model_dump(mode="json"))

    settings._bridge_transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
    decisions = HTTPDecisionBridge(settings, on_send=db.record_bridge_send)
    broker = SimulatedBroker()
    orchestrator = TradingOrchestrator(
        broker=broker,
        collector=MarketCollector(broker, settings),
        decisions=decisions,
        safety=SafetyEngine(settings),
        db=db,
        notifications=build_notification_service(settings, db),
    )
    result = await orchestrator.run_cycle(trigger="POLL")
    assert result is not None
    assert len(attempts) == 2, "flaky transport must exercise the retry path"
    executed = db.get_recent_decisions()
    executions = [row for row in executed if row.get("execution")]
    assert len(executions) == 1, "one cycle + retries must yield exactly one execution row"
    sends = db.get_bridge_sends(str(result.cycle_id))
    assert len(sends) == 2
    assert {s["attempt"] for s in sends} == {1, 2}
    assert len({s["message_sha256"] for s in sends}) == 1, "persisted sha must be stable across retries"