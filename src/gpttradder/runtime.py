from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .factory import build_orchestrator

logger = logging.getLogger(__name__)


async def run(settings: Settings, use_mock_decision: bool = False) -> None:
    orchestrator = build_orchestrator(settings, use_mock_decision=use_mock_decision)
    await orchestrator.broker.connect()
    await orchestrator.broker.assert_demo()
    logger.info("GPTTRADDER started: demo-only, symbols=%s", settings.symbols)

    # Immediate startup scan, then a simple deterministic interval loop.
    # Event-driven scans can be triggered independently through the local API.
    while True:
        await orchestrator.run_cycle(trigger="POLL", reason="scheduled_scan")
        await asyncio.sleep(settings.poll_seconds)
