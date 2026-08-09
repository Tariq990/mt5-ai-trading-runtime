from __future__ import annotations

import hashlib

from .models import MarketPacket


def market_packet_hash(packet: MarketPacket) -> str:
    payload = packet.model_dump_json(exclude={"packet_hash"}, by_alias=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
