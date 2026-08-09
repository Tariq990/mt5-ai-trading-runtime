from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .config import get_settings
from .factory import build_orchestrator

settings = get_settings()
orchestrator = build_orchestrator(settings)
app = FastAPI(title="GPTTRADDER", version="0.1.0")


@app.on_event("startup")
async def startup() -> None:
    await orchestrator.broker.connect()
    await orchestrator.broker.assert_demo()


@app.get("/health")
async def health():
    account = await orchestrator.broker.get_account_state()
    return {
        "ok": True,
        "demo": account.is_demo,
        "broker": settings.broker,
        "symbols": settings.symbols,
    }


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
