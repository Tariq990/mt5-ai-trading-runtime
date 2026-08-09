from __future__ import annotations

from gpttradder.watchdog import RestartBackoff, heartbeat_is_stale


def test_restart_backoff_is_exponential_and_capped():
    backoff = RestartBackoff(maximum=5)
    assert [backoff.fail() for _ in range(5)] == [1, 2, 4, 5, 5]
    backoff.success()
    assert backoff.fail() == 1


def test_heartbeat_staleness_honors_startup_grace():
    assert heartbeat_is_stale(None, 45, grace=True) is False
    assert heartbeat_is_stale(None, 45, grace=False) is True
    assert heartbeat_is_stale(44, 45) is False
    assert heartbeat_is_stale(46, 45) is True

import pytest
from gpttradder.config import Settings
from gpttradder.watchdog import RuntimeWatchdog


@pytest.mark.asyncio
async def test_watchdog_marks_missing_bridge_configuration(tmp_path, monkeypatch):
    settings = Settings(
        db_path=tmp_path / "watchdog.sqlite3",
        browser_mcp_dir=None,
        bridge_auto_start=True,
    )
    watchdog = RuntimeWatchdog(settings)

    async def unhealthy():
        return False

    monkeypatch.setattr(watchdog, "bridge_healthy", unhealthy)
    await watchdog.start_bridge()
    assert watchdog.db.get_state("bridge_status") == "down-browser-mcp-dir-missing"
