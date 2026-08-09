from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
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
    event_poll_seconds: int = Field(default=5, ge=1, le=60)
    event_move_bps: float = Field(default=20.0, gt=0, le=500)
    event_spread_multiplier: float = Field(default=2.5, gt=1, le=20)
    event_debounce_seconds: int = Field(default=30, ge=5, le=600)
    cycle_lock_ttl_seconds: int = Field(default=240, ge=30, le=600)
    risk_timezone: str = "Asia/Amman"
    # Canonical application instruments. The broker adapter maps each of these to
    # the actual broker symbol at runtime (mt5.symbols_get()). See symbols.py.
    symbols: list[str] = Field(default_factory=lambda: ["BTC", "ETH", "XAU", "EURUSD", "GBPUSD"])
    # Optional explicit canonical -> broker override for unusual suffixes, e.g.
    # GPTTRADDER_SYMBOL_MAP={"BTC":"BTCUSDm","XAU":"XAUUSD.a"}
    symbol_map: dict[str, str] = Field(default_factory=dict)
    candle_limit: int = Field(default=120, ge=30, le=1000)

    daily_loss_limit_pct: float = Field(default=3.0, gt=0, le=10)
    trailing_dd_limit_pct: float = Field(default=10.0, gt=0, le=25)
    packet_max_age_seconds: int = Field(default=90, ge=10, le=600)
    # Broker server clock offset from UTC. MT5 timelines (ticks, candles,
    # positions, deals) are stamped in broker server time — typically UTC+2
    # (EET) or UTC+3 (EEST). All broker timestamps are normalized to UTC by
    # this offset, and freshness gates fail closed on residual future skew.
    broker_server_utc_offset_hours: float = Field(default=2.0, ge=-14, le=14)
    # A quote stamped more than this far in the future (after normalization) is
    # treated as invalid: never fresh, never executable.
    quote_future_skew_tolerance_seconds: float = Field(default=5.0, ge=0, le=60)
    # Weekend session calendar for FX-calendar instruments (usd_fx, metal and
    # unclassified symbols). Closed from Friday `weekend_start_hour_utc` until
    # Sunday `weekend_end_hour_utc` (both UTC). Crypto runs 24/7 regardless.
    market_session_weekend_start_hour_utc: int = Field(default=21, ge=0, le=23)
    market_session_weekend_end_hour_utc: int = Field(default=21, ge=0, le=23)

    decision_bridge_url: str = "http://127.0.0.1:8787/decision"
    decision_timeout_seconds: int = Field(default=90, ge=5, le=300)
    decision_retry_delays: list[int] = Field(default_factory=lambda: [0, 15, 30, 60])

    # Local dashboard/API. The runtime binds to loopback by default so account
    # state is not exposed to the LAN.
    dashboard_enabled: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = Field(default=8765, ge=1024, le=65535)

    # Telegram alerts are optional until the user supplies BotFather credentials.
    telegram_enabled: bool = False
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    telegram_timeout_seconds: float = Field(default=10.0, gt=1, le=60)
    telegram_notify_wait: bool = False
    notification_cooldown_seconds: int = Field(default=300, ge=0, le=86400)

    # Daily operational/performance report.
    daily_report_enabled: bool = True
    daily_report_hour: int = Field(default=23, ge=0, le=23)
    daily_report_minute: int = Field(default=55, ge=0, le=59)

    # Runtime watchdog / zero-touch Windows startup.
    watchdog_poll_seconds: int = Field(default=10, ge=2, le=120)
    watchdog_heartbeat_seconds: int = Field(default=5, ge=1, le=60)
    watchdog_stale_seconds: int = Field(default=45, ge=10, le=600)
    watchdog_restart_backoff_max_seconds: int = Field(default=120, ge=5, le=1800)
    bridge_auto_start: bool = True
    browser_mcp_dir: Path | None = None
    chatgpt_session_key: str = "gpttradder"
    chatgpt_conversation_url: str | None = None
    # Fix 1 invariant: the inner ChatGPT/bridge timeout must stay STRICTLY below
    # the outer runtime decision timeout, or cycles hang. Enforced below.
    chatgpt_timeout_seconds: int = Field(default=80, ge=15, le=1800)
    # Forwarded to the browser MCP as CHATGPT_HEADLESS. Headless is the default
    # unless the machine's Cloudflare handling requires a visible window
    # (managed challenges can block headless Playwright Chromium outright).
    chatgpt_headless: bool = True

    # Deliberately impossible to enable from the MVP. The validator rejects true.
    allow_real_trading: bool = False

    @field_validator("symbols", mode="before")
    @classmethod
    def parse_symbols(cls, value):
        if isinstance(value, str):
            return [part.strip().upper() for part in value.split(",") if part.strip()]
        return value

    @field_validator("symbol_map", mode="before")
    @classmethod
    def parse_symbol_map(cls, value):
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"GPTTRADDER_SYMBOL_MAP must be valid JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ValueError("GPTTRADDER_SYMBOL_MAP must be a JSON object like {\"BTC\": \"BTCUSDm\"}")
            return {str(k).strip().upper(): str(v).strip().upper() for k, v in parsed.items() if str(k).strip() and str(v).strip()}
        if value:
            return {str(k).strip().upper(): str(v).strip().upper() for k, v in value.items() if str(k).strip() and str(v).strip()}
        return {}

    @model_validator(mode="after")
    def timeout_hierarchy(self) -> "Settings":
        # Fix 1 invariant (REPOSITORY.md): the inner ChatGPT/bridge timeout must
        # be strictly less than the outer runtime decision timeout or cycles hang.
        if self.chatgpt_timeout_seconds >= self.decision_timeout_seconds:
            raise ValueError(
                "GPTTRADDER_CHATGPT_TIMEOUT_SECONDS "
                f"({self.chatgpt_timeout_seconds}) must be strictly below "
                f"GPTTRADDER_DECISION_TIMEOUT_SECONDS ({self.decision_timeout_seconds})"
            )
        return self

    @field_validator("decision_retry_delays", mode="before")
    @classmethod
    def parse_retry_delays(cls, value):
        if isinstance(value, str):
            return [int(part.strip()) for part in value.split(",") if part.strip()]
        return value

    @field_validator("dashboard_host")
    @classmethod
    def loopback_dashboard_only(cls, value: str) -> str:
        if value not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Dashboard is intentionally restricted to loopback interfaces.")
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
