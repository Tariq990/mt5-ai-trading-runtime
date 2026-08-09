"""Regression tests for Settings loading from a dotenv file.

pydantic-settings JSON-decodes complex (list/dict) fields at the dotenv
source layer *before* field validators run, so a CSV value such as
``BTCUSD,XAUUSD`` raises SettingsError. The supported syntax is a JSON
array: ``["BTCUSD","XAUUSD"]``.
"""

from pathlib import Path

import pytest

from gpttradder.config import Settings


def test_dotenv_json_list_syntax_parses(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                'GPTTRADDER_SYMBOLS=["BTCUSD","XAUUSD"]',
                "GPTTRADDER_DECISION_RETRY_DELAYS=[0,15,30,60]",
            ]
        ),
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_file)
    assert settings.symbols == ["BTCUSD", "XAUUSD"]
    assert settings.decision_retry_delays == [0, 15, 30, 60]


def test_shipped_dotenv_parses() -> None:
    """The repo's own .env must never regress to CSV list syntax."""
    root = Path(__file__).resolve().parents[1]
    settings = Settings(_env_file=root / ".env")
    assert settings.symbols
    assert all(isinstance(s, str) and s.isupper() for s in settings.symbols)
    assert settings.decision_retry_delays
    assert all(isinstance(d, int) for d in settings.decision_retry_delays)


def test_demo_safety_contract() -> None:
    settings = Settings()
    assert settings.env == "demo"
    assert settings.allow_real_trading is False
    assert settings.daily_loss_limit_pct == 3.0
    assert settings.trailing_dd_limit_pct == 10.0


def test_timeout_hierarchy_rejects_chatgpt_timeout_at_or_above_decision_timeout() -> None:
    with pytest.raises(ValueError, match="strictly below"):
        Settings(chatgpt_timeout_seconds=90, decision_timeout_seconds=90)
    with pytest.raises(ValueError, match="strictly below"):
        Settings(chatgpt_timeout_seconds=120, decision_timeout_seconds=90)
    settings = Settings(chatgpt_timeout_seconds=80, decision_timeout_seconds=90)
    assert settings.chatgpt_timeout_seconds < settings.decision_timeout_seconds