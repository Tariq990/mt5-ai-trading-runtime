from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .base import DecisionProvider
from ..models import Decision, DecisionAction, MarketPacket


class WaitDecisionProvider(DecisionProvider):
    async def decide(self, packet: MarketPacket) -> Decision:
        return Decision(
            cycle_id=packet.cycle_id,
            decision=DecisionAction.WAIT,
            valid_until=datetime.now(timezone.utc) + timedelta(minutes=5),
            reason="Mock provider defaults to WAIT.",
            confidence=1.0,
        )
