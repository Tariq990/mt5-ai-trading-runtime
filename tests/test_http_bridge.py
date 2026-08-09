import uuid

import httpx
import pytest

from gpttradder.config import Settings
from gpttradder.decision.http_bridge import HTTPDecisionBridge
from gpttradder.models import AccountState, MarketPacket


CYCLE_ID = str(uuid.uuid4())


def _settings() -> Settings:
    return Settings(
        decision_bridge_url="http://127.0.0.1:1/decision",
        decision_retry_delays=[0, 1, 2],
        decision_timeout_seconds=90,
    )


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


def _bridge(transport: httpx.MockTransport) -> HTTPDecisionBridge:
    settings = _settings()
    settings._bridge_transport = transport  # type: ignore[attr-defined]
    return HTTPDecisionBridge(settings)


@pytest.mark.asyncio
async def test_decision_success_parses_bridge_json() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_decision_json())

    settings = _settings()
    settings._bridge_transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
    bridge = HTTPDecisionBridge(settings)
    decision = await bridge.decide(_packet())
    assert decision.decision == "WAIT"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_identical_error_body_fails_fast_without_wasted_retries() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(502, json={"error": "No valid Decision JSON object found"})

    settings = _settings()
    settings._bridge_transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
    bridge = HTTPDecisionBridge(settings)
    with pytest.raises(RuntimeError, match="No valid Decision JSON object found"):
        await bridge.decide(_packet())
    assert len(calls) == 2  # first attempt + one confirming retry, then fail fast


@pytest.mark.asyncio
async def test_changing_error_body_is_retried() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = "challenge" if len(calls) == 1 else "No valid Decision JSON object found"
        return httpx.Response(502, json={"error": body})

    settings = _settings()
    settings._bridge_transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
    bridge = HTTPDecisionBridge(settings)
    with pytest.raises(RuntimeError, match="HTTP 502"):
        await bridge.decide(_packet())
    assert len(calls) == 3  # body changed on retry, retried through the last delay


@pytest.mark.asyncio
async def test_http_status_error_surfaces_bridge_error_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": "browser died"})

    settings = _settings()
    settings._bridge_transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
    bridge = HTTPDecisionBridge(settings)
    with pytest.raises(RuntimeError, match="browser died"):
        await bridge.decide(_packet())


@pytest.mark.asyncio
async def test_invalid_decision_json_is_retried() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"decision": "WAIT"})  # invalid: no valid_until/cycle
        return httpx.Response(200, json=_decision_json())

    settings = _settings()
    settings._bridge_transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
    bridge = HTTPDecisionBridge(settings)
    decision = await bridge.decide(_packet())
    assert decision.decision == "WAIT"
    assert len(calls) == 2