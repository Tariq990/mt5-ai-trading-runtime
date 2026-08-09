from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .autostart import install_windows_autostart, remove_windows_autostart
from .broker.simulated import SimulatedBroker
from .collector import MarketCollector
from .config import get_settings
from .db import Database
from .decision.http_bridge import HTTPDecisionBridge
from .factory import build_broker, build_orchestrator
from .logging_setup import configure_logging
from .orchestrator import TradingOrchestrator
from .preflight import run_preflight
from .reports import build_daily_report, send_daily_report
from .runtime import run
from .safety import SafetyEngine
from .review import ReviewService
from .verification import verify_mt5_demo_write
from .watchdog import run_watchdog


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    parser = argparse.ArgumentParser(prog="gpttradder")
    parser.add_argument(
        "command",
        choices=[
            "run",
            "watchdog",
            "once",
            "smoke",
            "preflight",
            "verify-bridge",
            "verify-mt5-write",
            "report",
            "report-send",
            "review-test",
            "autostart-install",
            "autostart-remove",
        ],
    )
    parser.add_argument("--mock-decision", action="store_true", help="Use a WAIT-only local provider")
    parser.add_argument(
        "--symbol",
        default=None,
        help="Canonical symbol filter for verify-mt5-write (e.g. BTC or EURUSD)",
    )
    args = parser.parse_args()
    configure_logging(filename="watchdog.log" if args.command == "watchdog" else "gpttradder.log")
    settings = get_settings()

    if args.command == "autostart-install":
        print(install_windows_autostart())
        return
    if args.command == "autostart-remove":
        print(remove_windows_autostart())
        return

    async def _main():
        use_mock = args.mock_decision or os.environ.get("GPTTRADDER_MOCK_DECISION") == "1"
        if args.command == "run":
            await run(settings, use_mock_decision=use_mock)
            return

        if args.command == "watchdog":
            await run_watchdog(settings)
            return

        if args.command == "preflight":
            broker = build_broker(settings)
            checks = await run_preflight(settings, broker, Database(settings.db_path), check_bridge=not args.mock_decision)
            for check in checks:
                print(f"{'PASS' if check.ok else 'FAIL'}  {check.name}: {check.detail}")
            if not all(check.ok for check in checks):
                raise SystemExit(2)
            return

        if args.command == "verify-mt5-write":
            broker = build_broker(settings)
            from .broker.mt5_demo import MT5DemoBroker
            if not isinstance(broker, MT5DemoBroker):
                raise SystemExit("verify-mt5-write requires GPTTRADDER_BROKER=mt5")
            await broker.connect()
            result = await verify_mt5_demo_write(settings, broker, symbol_filter=args.symbol)
            print(result)
            return

        if args.command == "verify-bridge":
            # Safe end-to-end transport/schema test: ALWAYS uses the simulated broker,
            # regardless of GPTTRADDER_BROKER, so a ChatGPT LONG/SHORT cannot touch MT5.
            broker = SimulatedBroker()
            safe_db = Database(Path("state/bridge-verification.sqlite3"))
            from .notifications import build_notification_service
            orchestrator = TradingOrchestrator(
                broker=broker,
                collector=MarketCollector(broker, settings),
                decisions=HTTPDecisionBridge(settings),
                safety=SafetyEngine(settings),
                db=safe_db,
                notifications=build_notification_service(settings, safe_db),
            )
            await broker.connect()
            result = await orchestrator.run_cycle(trigger="POLL", reason="bridge_verification_simulated_only")
            print("NO_EXECUTION" if result is None else result.model_dump_json(indent=2))
            return

        if args.command in {"report", "report-send"}:
            db = Database(settings.db_path)
            report = build_daily_report(db, settings)
            if args.command == "report-send":
                from .notifications import build_notification_service
                report = await send_daily_report(
                    db,
                    settings,
                    build_notification_service(settings, db),
                    force=True,
                )
            print(report.text)
            return

        if args.command == "review-test":
            # End-to-end review channel check: sends a TEST event to the
            # separate gpttradder-review ChatGPT session. Advisory only —
            # no broker connection, no trading side effects.
            db = Database(settings.db_path)
            review = ReviewService(settings, db)
            if not review.enabled():
                raise SystemExit(
                    "Review channel is not enabled: set GPTTRADDER_REVIEW_ENABLED=true "
                    "and GPTTRADDER_CHATGPT_REVIEW_CONVERSATION_URL"
                )
            print(f"Review bridge: {settings.review_bridge_url}")
            print(f"Review session key: {settings.review_session_key}")
            data = await review.send_test_event()
            if data is None:
                raise SystemExit("Review TEST event was not delivered (duplicate or disabled)")
            print("OK — TEST event delivered and confirmed by the bridge.")
            print(f"ChatGPT response: {data.get('response') or '(empty)'}")
            return

        orchestrator = build_orchestrator(settings, use_mock_decision=use_mock or args.command == "smoke")
        await orchestrator.broker.connect()
        await orchestrator.broker.assert_demo()
        result = await orchestrator.run_cycle(trigger="POLL", reason=args.command)
        print("NO_EXECUTION" if result is None else result.model_dump_json(indent=2))

    asyncio.run(_main())


if __name__ == "__main__":
    main()
