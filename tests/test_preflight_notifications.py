from __future__ import annotations

import pytest

from gpttradder.broker.simulated import SimulatedBroker
from gpttradder.config import Settings
from gpttradder.db import Database
from gpttradder.preflight import run_preflight


@pytest.mark.asyncio
async def test_preflight_fails_when_telegram_enabled_without_credentials(tmp_path):
    settings = Settings(
        db_path=tmp_path / "preflight.sqlite3",
        telegram_enabled=True,
        telegram_bot_token=None,
        telegram_chat_id=None,
    )
    checks = await run_preflight(
        settings,
        SimulatedBroker(),
        Database(settings.db_path),
        check_bridge=False,
    )
    telegram = next(item for item in checks if item.name == "telegram")
    assert telegram.ok is False
    assert "missing" in telegram.detail.lower()
