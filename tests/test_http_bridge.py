import json
import uuid

import httpx
import pytest

from gpttradder.config import Settings
from gpttradder.decision.http_bridge import (
    DecisionBridgeError,
    STATE_DISPATCHED_UNCONFIRMED,
    STATE_FAILED_BEFORE_DISPATCH,
)
from gpttradder.models import AccountState, MarketPacket


CYCLE_ID = str(uuid.uuid4())


def _settings(**overrides) -> Settings:
    defaults = dict(
        decision_bridge_url="http://127.0.0.1:1/decision",
        decision_retry_delays=[0, 1, 1],
        decision_reconcile_interval_seconds=5,
        decision_timeout_seconds=90,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _packet() -> MarketPacket:
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
        broker_timestamp="2026-08-09T20:00:00+00:00",
        account=account,
        symbols={},
    )


def _decision_json(**overrides) -> dict:
    return {
        "decision_id": str(uuid.uuid4()),
        "cycle_id": CYCLE_ID,
        "decision": "WAIT",
        "valid_until": "2026-08-09T21:00:00+03:00",
        "confidence": 0.9,
        "reason": "no setup",
        **overrides,
    }


def _bridge(settings: Settings, transport: httpx.AsyncBaseTransport, on_send=None) -> "HTTPDecisionBridge":
    from gpttradder.decision.http_bridge import HTTPDecisionBridge

    settings._bridge_transport = transport  # type: ignore[attr-defined]
    return HTTPDecisionBridge(settings, on_send=on_send)


def _structured_502(**overrides) -> dict:
    body = dict(
        ok=False,
        error="ChatGPT bridge: failed_before_send (unexpected_error)",
        dispatch_state=STATE_FAILED_BEFORE_DISPATCH,
        retryable=True,
        status="failed_before_send",
        error_code="unexpected_error",
        cycle_id=CYCLE_ID,
        client_message_id=f"gpttradder:{CYCLE_ID}",
        message_sha256="sha",
        found_in_conversation=False,
        response_found=False,
    )
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_dispatch_succeeds_and_confirmation_succeeds() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_decision_json())

    bridge = _bridge(_settings(), httpx.MockTransport(handler))
    decision = await bridge.decide(_packet())
    assert decision.decision == "WAIT"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_failed_before_dispatch_is_retried_safely_then_succeeds() -> None:
    """The observed fill-timeout failure mode: nothing was dispatched, so the
    same frozen payload is re-sent until the composer recovers."""
    bodies = []
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(bytes(request.content))
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(502, json=_structured_502())
        return httpx.Response(200, json=_decision_json())

    bridge = _bridge(_settings(), httpx.MockTransport(handler))
    decision = await bridge.decide(_packet())
    assert decision.decision == "WAIT"
    assert len(calls) == 3
    # The frozen payload is byte-identical on every attempt: the same
    # client_message_id never carries divergent text (no duplicate turn risk).
    assert len(set(bodies)) == 1


@pytest.mark.asyncio
async def test_dispatch_unconfirmed_response_appears_after_lost_ack() -> None:
    """Dispatch evidence exists but the immediate confirmation timed out; the
    runtime must NEVER re-send — it reconciles by re-POSTing the same frozen
    payload, which the bridge resolves from the stored turn once the reply
    lands."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                502,
                json=_structured_502(
                    dispatch_state=STATE_DISPATCHED_UNCONFIRMED,
                    retryable=False,
                    status="unknown_after_send",
                    error_code="timeout",
                    found_in_conversation=True,
                ),
            )
        return httpx.Response(200, json=_decision_json())

    bridge = _bridge(_settings(), httpx.MockTransport(handler))
    decision = await bridge.decide(_packet())
    assert decision.decision == "WAIT"
    assert len(calls) == 2  # one dispatch + one reconciliation read, never more


@pytest.mark.asyncio
async def test_unconfirmed_that_never_resolves_fails_closed_with_audit() -> None:
    """If reconciliation cannot prove the reply within the bounded budget,
    trading must fail closed — never an unbounded wait, never a re-send."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            502,
            json=_structured_502(
                dispatch_state=STATE_DISPATCHED_UNCONFIRMED,
                retryable=False,
                status="unknown_after_send",
                error_code="timeout",
                found_in_conversation=True,
            ),
        )

    bridge = _bridge(
        _settings(
            chatgpt_timeout_seconds=15,
            decision_timeout_seconds=16,
            decision_retry_delays=[0],
            decision_reconcile_interval_seconds=5,
        ),
        httpx.MockTransport(handler),
    )
    packet = _packet()
    with pytest.raises(DecisionBridgeError) as excinfo:
        await bridge.decide(packet)
    audit = excinfo.value.audit
    assert audit["dispatch_state"] == STATE_DISPATCHED_UNCONFIRMED
    assert audit["cycle_id"] == str(packet.cycle_id)
    assert audit["send_attempt"] == len(calls)
    assert audit["reconcile_outcome"] == "UNRESOLVED"
    # Bounded: only the retry + reconciliation polls inside the budget.
    assert 1 <= len(calls) <= 6


@pytest.mark.asyncio
async def test_retry_after_confirmed_not_dispatched() -> None:
    """failed_before_send twice (proven not dispatched), then a successful
    re-dispatch of the same frozen payload."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) <= 2:
            return httpx.Response(502, json=_structured_502())
        return httpx.Response(200, json=_decision_json())

    bridge = _bridge(_settings(), httpx.MockTransport(handler))
    decision = await bridge.decide(_packet())
    assert decision.decision == "WAIT"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_non_retryable_pre_dispatch_rejection_fails_fast() -> None:
    """Deterministic pre-send rejection (e.g. frozen-message invariant breach):
    one attempt, immediate raise, full audit."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            502,
            json=_structured_502(retryable=False, error_code="message_mismatch"),
        )

    bridge = _bridge(_settings(), httpx.MockTransport(handler))
    with pytest.raises(DecisionBridgeError) as excinfo:
        await bridge.decide(_packet())
    assert excinfo.value.audit["dispatch_state"] == STATE_FAILED_BEFORE_DISPATCH
    assert excinfo.value.audit["reconcile_outcome"] == "REJECTED_BEFORE_DISPATCH"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_invalid_decision_from_proven_reply_fails_closed_immediately() -> None:
    """A 200 with an unparseable decision cannot be re-dispatched (the turn is
    complete); fail closed at once."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"decision": "WAIT"})  # missing cycle_id/valid_until

    bridge = _bridge(_settings(), httpx.MockTransport(handler))
    with pytest.raises(DecisionBridgeError) as excinfo:
        await bridge.decide(_packet())
    assert excinfo.value.audit["dispatch_state"] == "RESPONSE_RECEIVED"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_legacy_unstructured_502_fails_closed_without_resending() -> None:
    """A bridge response without a structured dispatch_state cannot prove
    anything: never guess a fresh dispatch, reconcile within the budget, then
    fail closed."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(502, json={"error": "No valid Decision JSON object found"})

    bridge = _bridge(
        _settings(
            chatgpt_timeout_seconds=15,
            decision_timeout_seconds=16,
            decision_retry_delays=[0],
            decision_reconcile_interval_seconds=5,
        ),
        httpx.MockTransport(handler),
    )
    with pytest.raises(DecisionBridgeError) as excinfo:
        await bridge.decide(_packet())
    assert excinfo.value.audit["dispatch_state"] == "NOT_FOUND"
    assert excinfo.value.audit["reconcile_outcome"] == "UNRESOLVED"
    assert len(calls) <= 6


@pytest.mark.asyncio
async def test_bridge_restart_during_reconciliation_recovers() -> None:
    """The bridge dies between a dispatched-but-unconfirmed turn and the reply:
    transport errors are retryable (nothing dispatched by the failed call) and
    the next POST resolves the stored turn."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                502,
                json=_structured_502(
                    dispatch_state=STATE_DISPATCHED_UNCONFIRMED,
                    retryable=False,
                    status="unknown_after_send",
                    error_code="timeout",
                    found_in_conversation=True,
                ),
            )
        if len(calls) == 2:
            raise httpx.ConnectError("connection refused (bridge restarting)")
        return httpx.Response(200, json=_decision_json())

    bridge = _bridge(_settings(), httpx.MockTransport(handler))
    decision = await bridge.decide(_packet())
    assert decision.decision == "WAIT"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_on_send_records_dispatch_state_per_attempt() -> None:
    records = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_decision_json())

    def on_send(**kwargs) -> None:
        records.append(kwargs)

    packet = _packet()
    bridge = _bridge(_settings(), httpx.MockTransport(handler), on_send=on_send)
    await bridge.decide(packet)
    assert len(records) == 1
    assert records[0]["dispatch_state"] == "RESPONSE_RECEIVED"
    assert records[0]["outcome"] == "SUCCESS"
    assert records[0]["response_found"] is True
    assert records[0]["cycle_id"] == str(packet.cycle_id)
    assert records[0]["client_message_id"] == f"gpttradder:{packet.cycle_id}"
    assert records[0]["message_sha256"]


@pytest.mark.asyncio
async def test_frozen_payload_never_diverges_across_attempts() -> None:
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content.decode("utf-8"))
        return httpx.Response(502, json=_structured_502())

    bridge = _bridge(_settings(), httpx.MockTransport(handler))
    with pytest.raises(DecisionBridgeError):
        await bridge.decide(_packet())
    assert len(bodies) > 1
    payloads = [json.loads(b) for b in bodies]
    # Every attempt carries the identical message payload and message_sha256.
    assert len({b for b in bodies}) == 1
    assert len({p["message_sha256"] for p in payloads}) == 1
    assert len({p["market_packet"]["cycle_id"] for p in payloads}) == 1
