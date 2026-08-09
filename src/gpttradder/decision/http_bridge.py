from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Callable

import httpx

from .base import DecisionProvider
from ..config import Settings
from ..models import Decision, MarketPacket


class HTTPDecisionBridge(DecisionProvider):
    """Adapter to the user's existing browser/Playwright bridge.

    GPTTRADDER posts a MarketPacket to a LOCAL bridge endpoint and expects the final
    structured Decision JSON in response. The bridge implementation stays separate,
    so browser/session details and cookies never enter this repository.

    Idempotency contract (Fix 4): the complete payload is FROZEN once per cycle —
    one canonical serialization, one SHA-256 fingerprint, one client_message_id
    (``gpttradder:<cycle_id>``). Every retry reuses the byte-identical frozen body;
    the digest it renders is deterministic (ages come from the fixed packet
    reference instant), so the ChatGPT bridge's dedup can never see the same
    client_message_id with different text (that mismatch was the root cause of the
    repeated 502 storms). Each send attempt is optionally persisted via `on_send`.
    """

    def __init__(self, settings: Settings, on_send: Callable[..., None] | None = None):
        self.settings = settings
        self.on_send = on_send
        self._frozen: dict[str, bytes] = {}
        self._frozen_sha: dict[str, str] = {}

    def freeze(self, packet: MarketPacket) -> tuple[bytes, str, str]:
        """Serialize the payload exactly once for a cycle.

        Returns (payload_bytes, message_sha256, client_message_id). Repeated
        calls return the byte-identical cached payload; calling with DIFFERENT
        packet content for the same cycle_id raises — a cycle's message is
        immutable once frozen, so the same client_message_id can never carry
        divergent text.
        """
        cycle_id = packet.cycle_id
        base = {
            "task": "TRADE_DECISION_REQUEST",
            "constraints": {
                "decision_owner": "ChatGPT",
                "deepseek_role": "orchestration_only",
                "demo_only": True,
                "return": "Decision JSON only",
            },
            "market_packet": packet.model_dump(mode="json"),
        }
        base_bytes = json.dumps(base, sort_keys=True, separators=(",", ":")).encode("utf-8")
        message_sha256 = hashlib.sha256(base_bytes).hexdigest()
        if cycle_id in self._frozen:
            if self._frozen_sha[cycle_id] != message_sha256:
                raise RuntimeError(
                    f"cycle {cycle_id} is already frozen with a different message "
                    f"(sha {self._frozen_sha[cycle_id]} != {message_sha256}): refusing to send "
                    "divergent text for the same client_message_id"
                )
            return self._frozen[cycle_id], self._frozen_sha[cycle_id], f"gpttradder:{cycle_id}"
        payload = {"message_sha256": message_sha256, **base}
        payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._frozen[cycle_id] = payload_bytes
        self._frozen_sha[cycle_id] = message_sha256
        return payload_bytes, message_sha256, f"gpttradder:{cycle_id}"

    def assert_frozen(self, packet: MarketPacket) -> None:
        """Fail closed if a cycle is being resent with DIFFERENT packet content."""
        self.freeze(packet)

    async def decide(self, packet: MarketPacket) -> Decision:
        payload_bytes, message_sha256, client_message_id = self.freeze(packet)
        last_error: Exception | None = None
        last_body = ""
        for attempt, delay in enumerate(self.settings.decision_retry_delays, start=1):
            if delay:
                await asyncio.sleep(delay)
            if self.on_send:
                self.on_send(
                    cycle_id=packet.cycle_id,
                    client_message_id=client_message_id,
                    message_sha256=message_sha256,
                    attempt=attempt,
                )
            try:
                async with httpx.AsyncClient(
                    timeout=self.settings.decision_timeout_seconds,
                    transport=getattr(self.settings, "_bridge_transport", None),
                ) as client:
                    response = await client.post(
                        self.settings.decision_bridge_url,
                        content=payload_bytes,
                        headers={"content-type": "application/json"},
                    )
                    response.raise_for_status()
                    return Decision.model_validate(response.json())
            except httpx.HTTPStatusError as exc:
                body = exc.response.text[:400]
                last_error = RuntimeError(f"HTTP {exc.response.status_code}: {body or exc}")
                # Deterministic failure (identical server error twice in a row)
                # will never succeed on further retries; fail fast.
                if body and body == last_body:
                    raise last_error
                last_body = body
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
        raise RuntimeError(f"Decision bridge failed after retries: {last_error}")