from __future__ import annotations

import asyncio

import httpx

from .base import DecisionProvider
from ..config import Settings
from ..models import Decision, MarketPacket


class HTTPDecisionBridge(DecisionProvider):
    """Adapter to the user's existing browser/Playwright bridge.

    GPTTRADDER posts a MarketPacket to a LOCAL bridge endpoint and expects the final
    structured Decision JSON in response. The bridge implementation stays separate,
    so browser/session details and cookies never enter this repository.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    async def decide(self, packet: MarketPacket) -> Decision:
        last_error: Exception | None = None
        last_body = ""
        for delay in self.settings.decision_retry_delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                async with httpx.AsyncClient(
                    timeout=self.settings.decision_timeout_seconds,
                    transport=getattr(self.settings, "_bridge_transport", None),
                ) as client:
                    response = await client.post(
                        self.settings.decision_bridge_url,
                        json={
                            "task": "TRADE_DECISION_REQUEST",
                            "constraints": {
                                "decision_owner": "ChatGPT",
                                "deepseek_role": "orchestration_only",
                                "demo_only": True,
                                "return": "Decision JSON only",
                            },
                            "market_packet": packet.model_dump(mode="json"),
                        },
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
