from __future__ import annotations

from fastapi.testclient import TestClient

from gpttradder.api import create_app
from gpttradder.broker.simulated import SimulatedBroker
from gpttradder.collector import MarketCollector
from gpttradder.config import Settings
from gpttradder.db import Database
from gpttradder.factory import build_orchestrator


def test_dashboard_and_dashboard_api_work_without_external_assets(tmp_path):
    settings = Settings(db_path=tmp_path / "dashboard.sqlite3", dashboard_enabled=True, broker="simulated")
    orchestrator = build_orchestrator(settings, use_mock_decision=True)
    app = create_app(settings, orchestrator, manage_broker=True)
    with TestClient(app) as client:
        html = client.get("/dashboard")
        assert html.status_code == 200
        assert "GPTTRADDER" in html.text
        assert "setInterval(refresh,5000)" in html.text
        assert "https://" not in html.text

        data = client.get("/api/dashboard")
        assert data.status_code == 200
        payload = data.json()
        assert "recent_decisions" in payload
        assert "runtime_heartbeat_age" in payload
        assert "bridge_status" in payload


def test_dashboard_snapshot_serves_multiple_symbols(tmp_path):
    settings = Settings(db_path=tmp_path / "dashboard2.sqlite3", broker="simulated")
    broker = SimulatedBroker()
    db = Database(settings.db_path)
    collector = MarketCollector(broker, settings)

    import asyncio

    packet = asyncio.new_event_loop().run_until_complete(collector.collect())
    db.save_cycle(packet, packet_hash="test")
    snapshot = db.dashboard_snapshot()
    assert set(snapshot["symbols"]) == {"BTC", "ETH", "XAU", "EURUSD", "GBPUSD"}
    assert snapshot["symbols"]["BTC"]["quote"]["bid"] > 0
    assert snapshot["symbols"]["BTC"]["contract"]["broker_symbol"] == "BTCUSD"
    assert snapshot["symbols"]["XAU"]["contract"]["broker_symbol"] == "XAUUSD"


def test_dashboard_host_is_loopback_only():
    try:
        Settings(dashboard_host="0.0.0.0")
    except ValueError as exc:
        assert "loopback" in str(exc).lower()
    else:
        raise AssertionError("public dashboard bind should be rejected")
