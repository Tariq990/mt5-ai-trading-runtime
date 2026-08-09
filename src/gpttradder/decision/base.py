from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Decision, MarketPacket


class DecisionProvider(ABC):
    @abstractmethod
    async def decide(self, packet: MarketPacket) -> Decision:
        """Return one strict structured trading decision for the supplied cycle."""
        ...
