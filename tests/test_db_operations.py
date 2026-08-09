from __future__ import annotations

import time

from gpttradder.db import Database


def test_heartbeat_age_and_dashboard_snapshot(tmp_path):
    db = Database(tmp_path / "state.sqlite3")
    assert db.heartbeat_age("runtime") is None
    db.heartbeat("runtime", {"pid": 123})
    age = db.heartbeat_age("runtime")
    assert age is not None and age < 1
    snapshot = db.dashboard_snapshot(timezone_name="UTC")
    assert snapshot["runtime_heartbeat_age"] is not None
    assert snapshot["total_cycles"] == 0
