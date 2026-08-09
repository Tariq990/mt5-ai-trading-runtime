from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from gpttradder.models import (
    Decision,
    DecisionAction,
    ManagementAction,
    ManagementInstruction,
    OrderInstruction,
    OrderType,
)


def until():
    return datetime.now(timezone.utc) + timedelta(minutes=5)


def test_pending_entry_requires_entry_price():
    with pytest.raises(ValidationError):
        OrderInstruction(type=OrderType.LIMIT, size=0.1)


def test_entry_requires_risk_percent():
    with pytest.raises(ValidationError):
        Decision(
            cycle_id=uuid4(), decision=DecisionAction.LONG, symbol="BTCUSD",
            order=OrderInstruction(type=OrderType.MARKET, size=0.1), stop_loss=64000,
            valid_until=until(), reason="test",
        )


def test_manage_position_requires_valid_instruction():
    decision = Decision(
        cycle_id=uuid4(), decision=DecisionAction.MANAGE_POSITION, symbol="BTCUSD",
        management=ManagementInstruction(action=ManagementAction.BREAK_EVEN, position_id="42"),
        valid_until=until(), reason="protect",
    )
    assert decision.management.position_id == "42"


def test_partial_close_requires_less_than_100_percent():
    with pytest.raises(ValidationError):
        ManagementInstruction(action=ManagementAction.PARTIAL_CLOSE, position_id="42", close_percent=100)
