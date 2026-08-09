from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="GPTTRADDER_",
        extra="ignore",
    )

    env: Literal["demo"] = "demo"
    broker: Literal["simulated", "mt5"] = "simulated"
    db_path: Path = Path("state/gpttradder.sqlite3")
    poll_seconds: int = Field(default=300, ge=15)
    symbols: list[str] = Field(default_factory=lambda: ["BTCUSD", "XAUUSD"])
    candle_limit: int = Field(default=120, ge=30, le=1000)

    daily_loss_limit_pct: float = Field(default=3.0, gt=0, le=10)
    trailing_dd_limit_pct: float = Field(default=10.0, gt=0, le=25)
    packet_max_age_seconds: int = Field(default=90, ge=10, le=600)

    decision_bridge_url: str = "http://127.0.0.1:8787/decision"
    decision_timeout_seconds: int = Field(default=90, ge=5, le=300)
    decision_retry_delays: list[int] = Field(default_factory=lambda: [0, 15, 30, 60])

    # Deliberately impossible to enable from the MVP. The validator rejects true.
    allow_real_trading: bool = False

    @field_validator("symbols", mode="before")
    @classmethod
    def parse_symbols(cls, value):
        if isinstance(value, str):
            return [part.strip().upper() for part in value.split(",") if part.strip()]
        return value

    @field_validator("decision_retry_delays", mode="before")
    @classmethod
    def parse_retry_delays(cls, value):
        if isinstance(value, str):
            return [int(part.strip()) for part in value.split(",") if part.strip()]
        return value

    @field_validator("allow_real_trading")
    @classmethod
    def forbid_real_trading(cls, value: bool) -> bool:
        if value:
            raise ValueError("GPTTRADDER MVP is DEMO ONLY; real trading is hard-disabled.")
        return False


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
