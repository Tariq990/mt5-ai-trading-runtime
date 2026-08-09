from fastapi.testclient import TestClient


def test_health_endpoint_works_with_default_simulator(tmp_path, monkeypatch):
    monkeypatch.setenv("GPTTRADDER_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("GPTTRADDER_BROKER", "simulated")
    # Import inside test after environment is set.
    import importlib
    import gpttradder.config as config
    config.get_settings.cache_clear()
    import gpttradder.api as api
    api = importlib.reload(api)
    with TestClient(api.app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["demo"] is True
