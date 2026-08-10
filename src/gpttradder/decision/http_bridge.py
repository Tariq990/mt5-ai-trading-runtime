from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Callable

import httpx

from .base import DecisionProvider
from ..config import Settings
from ..models import Decision, MarketPacket


class DecisionBridgeError(RuntimeError):
    """A decision-provider failure carrying the full dispatch audit trail.

    ``audit`` never contains secrets or browser state: only cycle_id,
    client_message_id, message_sha256, dispatch_state, send_attempt,
    found_in_conversation, response_found and the final reconciliation
    outcome — the exact fields the review channel needs to diagnose a
    failed cycle without exposing anything sensitive.
    """

    def __init__(self, message: str, *, audit: dict[str, Any] | None = None):
        super().__init__(message)
        self.audit = audit or {}


# Canonical dispatch states (mirror of bridge/dispatch-state.mjs).
STATE_RESPONSE_RECEIVED = "RESPONSE_RECEIVED"
STATE_DISPATCHED_CONFIRMED = "DISPATCHED_CONFIRMED"
STATE_DISPATCHED_UNCONFIRMED = "DISPATCHED_UNCONFIRMED"
STATE_FAILED_BEFORE_DISPATCH = "FAILED_BEFORE_DISPATCH"
STATE_NOT_FOUND = "NOT_FOUND"

# Non-retryable dispatch states: the message may already exist in the
# conversation (or the store cannot prove otherwise). Never dispatch anew —
# only bounded reconciliation reads are allowed.
RECONCILE_STATES = {STATE_DISPATCHED_CONFIRMED, STATE_DISPATCHED_UNCONFIRMED, STATE_NOT_FOUND}


class HTTPDecisionBridge(DecisionProvider):
    """Adapter to the user's existing browser/Playwright bridge.

    GPTTRADDER posts a MarketPacket to a LOCAL bridge endpoint and expects the
    final structured Decision JSON in response. The bridge implementation stays
    separate, so browser/session details and cookies never enter this
    repository.

    Idempotency contract (Fix 4): the complete payload is FROZEN once per
    cycle — one canonical serialization, one SHA-256 fingerprint, one
    client_message_id (``gpttradder:<cycle_id>``). Every retry reuses the
    byte-identical frozen body; the digest it renders is deterministic (ages
    come from the fixed packet reference instant), so the ChatGPT bridge's
    dedup can never see the same client_message_id with different text (that
    mismatch was the root cause of the repeated 502 storms). Each send attempt
    is optionally persisted via `on_send`.

    Dispatch-state contract (Fix 6): the bridge now classifies every failed
    outcome into an explicit dispatch_state instead of a blanket 502:
      FAILED_BEFORE_DISPATCH  -> nothing reached the browser; retry is safe
      DISPATCHED_UNCONFIRMED  -> durable evidence proves the message reached
                                 the conversation; NEVER send again, only
                                 reconcile (bounded polling for the reply)
      DISPATCHED_CONFIRMED    -> backend confirmed; reply pending, reconcile
      NOT_FOUND               -> cannot prove either way; fail closed
      RESPONSE_RECEIVED       -> reply proven, decision expected
    Reconciliation re-POSTs the SAME frozen payload; the bridge/MCP dedup
    returns the stored turn (cached read) and never creates a second ChatGPT
    turn. If reconciliation cannot prove a reply within the bounded budget,
    the cycle fails closed with a full audit trail.
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
        last_audit: dict[str, Any] = {}
        delays = list(self.settings.decision_retry_delays)
        loop = asyncio.get_running_loop()
        # Hard wall-clock bound: retry delays + a reconciliation window. After
        # this, ANY unresolved state fails closed — never an unbounded wait.
        deadline = loop.time() + float(self.settings.decision_timeout_seconds) + float(
            self.settings.decision_reconcile_interval_seconds
        )
        attempt = 0
        while True:
            delay = delays.pop(0) if delays else self.settings.decision_reconcile_interval_seconds
            if delay:
                await asyncio.sleep(delay)
            attempt += 1

            audit, decision, hard_error = await self._post_once(
                packet=packet,
                payload_bytes=payload_bytes,
                client_message_id=client_message_id,
                message_sha256=message_sha256,
                attempt=attempt,
            )
            last_audit = audit
            if self.on_send is not None:
                self.on_send(
                    cycle_id=audit.get("cycle_id"),
                    client_message_id=audit.get("client_message_id"),
                    message_sha256=audit.get("message_sha256"),
                    attempt=audit.get("send_attempt"),
                    dispatch_state=audit.get("dispatch_state"),
                    error_code=audit.get("error_code"),
                    outcome=audit.get("outcome"),
                    response_found=audit.get("response_found"),
                )

            if decision is not None:
                return decision
            if hard_error is not None:
                raise hard_error

            if loop.time() >= deadline:
                raise DecisionBridgeError(
                    f"Decision bridge unresolved after {attempt} attempt(s): "
                    f"last state {audit.get('dispatch_state')} ({audit.get('error_code') or 'no error code'})",
                    audit={**last_audit, "reconcile_outcome": "UNRESOLVED"},
                )

    async def _post_once(
        self,
        *,
        packet: MarketPacket,
        payload_bytes: bytes,
        client_message_id: str,
        message_sha256: str,
        attempt: int,
    ) -> tuple[dict[str, Any], Decision | None, DecisionBridgeError | None]:
        """One POST against the bridge. Returns (audit, decision, hard_error).

        Never raises transport errors: a failed HTTP call is classified as
        FAILED_BEFORE_DISPATCH (nothing reached the browser) and is retried —
        the MCP dedup makes that safe even after a bridge restart.
        """
        audit: dict[str, Any] = {
            "cycle_id": str(packet.cycle_id),
            "client_message_id": client_message_id,
            "message_sha256": message_sha256,
            "send_attempt": attempt,
        }
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
        except httpx.HTTPError as exc:
            audit.update(
                {
                    "dispatch_state": STATE_FAILED_BEFORE_DISPATCH,
                    "error_code": "http_error",
                    "outcome": "ERROR",
                    "found_in_conversation": False,
                    "response_found": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return audit, None, None

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except ValueError:
            body = {"error": response.text[:300]}

        if response.status_code == 200:
            try:
                decision = Decision.model_validate(body)
            except ValueError as exc:
                audit.update(
                    {
                        "dispatch_state": STATE_RESPONSE_RECEIVED,
                        "error_code": "invalid_decision",
                        "outcome": "FAILED",
                        "found_in_conversation": True,
                        "response_found": True,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                # A proven reply can never be re-dispatchable; fail closed now.
                return audit, None, DecisionBridgeError(
                    f"Bridge returned an invalid Decision: {exc}",
                    audit={**audit, "reconcile_outcome": "INVALID_DECISION"},
                )
            audit.update(
                {
                    "dispatch_state": STATE_RESPONSE_RECEIVED,
                    "error_code": None,
                    "outcome": "SUCCESS",
                    "found_in_conversation": True,
                    "response_found": True,
                }
            )
            return audit, decision, None

        # Non-200. Prefer the bridge's structured dispatch state; fall back to
        # a conservative NOT_FOUND (never guess a fresh dispatch).
        state = body.get("dispatch_state")
        retryable = body.get("retryable")
        if state not in {STATE_RESPONSE_RECEIVED, STATE_DISPATCHED_CONFIRMED, STATE_DISPATCHED_UNCONFIRMED, STATE_FAILED_BEFORE_DISPATCH, STATE_NOT_FOUND}:
            state = STATE_NOT_FOUND
            retryable = False
        audit.update(
            {
                "dispatch_state": state,
                "error_code": body.get("error_code"),
                "outcome": "ERROR" if retryable else "UNCONFIRMED",
                "found_in_conversation": bool(body.get("found_in_conversation", False)),
                "response_found": bool(body.get("response_found", False)),
                "error": body.get("error") or response.text[:300],
            }
        )

        if state == STATE_FAILED_BEFORE_DISPATCH and not retryable:
            # Deterministic pre-send rejection (e.g. frozen-message invariant
            # breach): further attempts can never succeed. Fail fast.
            return audit, None, DecisionBridgeError(
                f"Bridge rejected the send before dispatch (non-retryable): {audit['error']}",
                audit={**audit, "reconcile_outcome": "REJECTED_BEFORE_DISPATCH"},
            )
        if state == STATE_RESPONSE_RECEIVED:
            # The bridge received a reply but could not validate it.
            return audit, None, DecisionBridgeError(
                f"Bridge received a response but it was not usable: {audit['error']}",
                audit={**audit, "reconcile_outcome": "RESPONSE_UNUSABLE"},
            )
        # FAILED_BEFORE_DISPATCH (retryable) -> retry; RECONCILE_STATES ->
        # bounded reconciliation re-POSTs (cached reads, never a new turn).
        return audit, None, None
