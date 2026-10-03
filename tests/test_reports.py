from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gpttradder.broker.simulated import SimulatedBroker
from gpttradder.collector import MarketCollector
from gpttradder.config import Settings
from gpttradder.db import Database
from gpttradder.decision.mock import WaitDecisionProvider
from gpttradder.notifications import NotificationService
from gpttradder.orchestrator import TradingOrchestrator
from gpttradder.reports import build_daily_report, seconds_until_report, send_daily_report
from gpttradder.safety import SafetyEngine


async def create_wait_cycle(tmp_path):
    settings = Settings(db_path=tmp_path / "report.sqlite3", telegram_enabled=False, symbols=["BTC", "ETH"])
    broker = SimulatedBroker()
    db = Database(settings.db_path)
    orchestrator = TradingOrchestrator(
        broker,
        MarketCollector(broker, settings),
        WaitDecisionProvider(),
        SafetyEngine(settings),
        db,
    )
    await broker.connect()
    result = await orchestrator.run_cycle()
    assert result is not None and result.status == "SKIPPED"
    return settings, db


@pytest.mark.asyncio
async def test_daily_report_uses_durable_decisions_and_is_idempotent(tmp_path):
    settings, db = await create_wait_cycle(tmp_path)
    report = build_daily_report(db, settings)
    assert "Decisions: 1" in report.text
    assert "WAIT: 1" in report.text
    assert "Market state:" in report.text
    assert "- BTC (BTCUSD): TRADABLE" in report.text
    assert "- ETH (ETHUSD): TRADABLE" in report.text

    class FakeNotifier:
        def __init__(self):
            self.sent = []

        async def healthcheck(self):
            return True, "ok"

        async def send(self, text):
            self.sent.append(text)
            return True

    fake = FakeNotifier()
    service = NotificationService(fake, db)
    await send_daily_report(db, settings, service)
    await send_daily_report(db, settings, service)
    assert len(fake.sent) == 1
    stored = db.get_daily_report(report.report_date)
    assert stored is not None
    assert stored["sent"] == 1


def test_seconds_until_report_rolls_to_next_day():
    settings = Settings(daily_report_hour=23, daily_report_minute=55, risk_timezone="UTC")
    before = datetime(2026, 8, 9, 23, 50, tzinfo=timezone.utc)
    after = datetime(2026, 8, 9, 23, 56, tzinfo=timezone.utc)
    assert seconds_until_report(settings, before) == pytest.approx(300)
    assert seconds_until_report(settings, after) == pytest.approx(23 * 3600 + 59 * 60)
