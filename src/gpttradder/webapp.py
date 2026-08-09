from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .config import Settings, get_settings
from .dashboard import dashboard_html
from .factory import build_orchestrator
from .orchestrator import TradingOrchestrator
from .reports import build_daily_report


def create_app(
    settings: Settings | None = None,
    orchestrator: TradingOrchestrator | None = None,
    *,
    manage_broker: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    orchestrator = orchestrator or build_orchestrator(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if manage_broker:
            await orchestrator.broker.connect()
            await orchestrator.broker.assert_demo()
        yield

    app = FastAPI(title="GPTTRADDER", version="0.4.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.orchestrator = orchestrator

    @app.get("/health")
    async def health():
        account = await orchestrator.broker.get_account_state()
        return {
            "ok": True,
            "demo": account.is_demo,
            "broker": settings.broker,
            "symbols": settings.symbols,
            "runtime_heartbeat_age": orchestrator.db.heartbeat_age("runtime"),
        }

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard():
        return dashboard_html()

    @app.get("/api/dashboard")
    async def dashboard_data():
        return orchestrator.db.dashboard_snapshot(limit=30, timezone_name=settings.risk_timezone)

    @app.get("/api/report/today")
    async def today_report():
        report = build_daily_report(orchestrator.db, settings)
        return {"report_date": report.report_date, "text": report.text, "payload": report.payload}

    @app.post("/cycle")
    async def cycle():
        result = await orchestrator.run_cycle(trigger="EVENT", reason="manual_api_trigger")
        if result is None:
            return {"status": "NO_EXECUTION"}
        return result.model_dump(mode="json")

    @app.post("/event/{reason}")
    async def event(reason: str):
        if len(reason) > 120:
            raise HTTPException(status_code=400, detail="reason too long")
        result = await orchestrator.run_cycle(trigger="EVENT", reason=reason)
        return {"result": None if result is None else result.model_dump(mode="json")}

    return app

