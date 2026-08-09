from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import uvicorn

from .webapp import create_app
from .config import Settings
from .event_monitor import MarketEventMonitor
from .factory import build_orchestrator
from .preflight import run_preflight
from .reports import daily_report_loop

logger = logging.getLogger(__name__)


async def _heartbeat_loop(orchestrator, settings: Settings) -> None:
    while True:
        orchestrator.db.heartbeat(
            "runtime",
            {
                "pid": os.getpid(),
                "broker": settings.broker,
                "symbols": settings.symbols,
            },
        )
        await asyncio.sleep(settings.watchdog_heartbeat_seconds)


async def _serve_dashboard(orchestrator, settings: Settings) -> None:
    app = create_app(settings, orchestrator, manage_broker=False)
    config = uvicorn.Config(
        app,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    await server.serve()
    if not server.should_exit:
        raise RuntimeError("Dashboard server stopped unexpectedly")


async def run(settings: Settings, use_mock_decision: bool = False) -> None:
    orchestrator = build_orchestrator(settings, use_mock_decision=use_mock_decision)

    # Direct `run` is safe on its own: before the continuous loop starts, verify
    # the Demo account/market/bridge (bridge skipped only for the WAIT-only mock).
    checks = await run_preflight(
        settings,
        orchestrator.broker,
        orchestrator.db,
        check_bridge=not use_mock_decision,
    )
    failures = [check for check in checks if not check.ok]
    if failures:
        detail = "; ".join(f"{item.name}: {item.detail}" for item in failures)
        raise RuntimeError(f"Startup preflight failed: {detail}")

    await orchestrator.broker.assert_demo()
    orchestrator.db.heartbeat("runtime", {"pid": os.getpid(), "status": "starting"})
    logger.info("GPTTRADDER started: demo-only, symbols=%s", settings.symbols)
    if orchestrator.notifications:
        await orchestrator.notifications.runtime(
            f"Runtime started on {settings.broker}; symbols={','.join(settings.symbols)}",
            key="runtime-started",
        )

    monitor = MarketEventMonitor(orchestrator.broker, orchestrator, settings)

    async def scheduled_loop() -> None:
        while True:
            await orchestrator.run_cycle(trigger="POLL", reason="scheduled_scan")
            await asyncio.sleep(settings.poll_seconds)

    async def review_loop() -> None:
        review = orchestrator.review
        if review is None:
            return
        while True:
            await asyncio.sleep(settings.review_check_seconds)
            await review.process_pending(now=datetime.now(timezone.utc))

    tasks = [
        asyncio.create_task(scheduled_loop(), name="scheduled-cycle"),
        asyncio.create_task(monitor.run(), name="event-monitor"),
        asyncio.create_task(_heartbeat_loop(orchestrator, settings), name="runtime-heartbeat"),
        asyncio.create_task(review_loop(), name="review-channel"),
    ]
    if settings.daily_report_enabled and orchestrator.notifications:
        tasks.append(
            asyncio.create_task(
                daily_report_loop(orchestrator.db, settings, orchestrator.notifications),
                name="daily-report",
            )
        )
    if settings.dashboard_enabled:
        tasks.append(
            asyncio.create_task(_serve_dashboard(orchestrator, settings), name="dashboard")
        )

    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        orchestrator.db.set_state("runtime_status", "stopped")
        if orchestrator.notifications:
            await orchestrator.notifications.runtime(
                "Runtime stopped.", severity="WARNING", key="runtime-stopped"
            )
