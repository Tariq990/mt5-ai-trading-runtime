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
        for delay in self.settings.decision_retry_delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                async with httpx.AsyncClient(timeout=self.settings.decision_timeout_seconds) as client:
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
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
        raise RuntimeError(f"Decision bridge failed after retries: {last_error}")
