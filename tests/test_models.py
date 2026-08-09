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


def test_minmax_dict_price_range_coerced_to_tuple():
    order = OrderInstruction(type=OrderType.MARKET, size=0.1, acceptable_price_range={"min": 65160, "max": 65185})
    assert order.acceptable_price_range == (65160.0, 65185.0)


def test_partial_close_requires_less_than_100_percent():
    with pytest.raises(ValidationError):
        ManagementInstruction(action=ManagementAction.PARTIAL_CLOSE, position_id="42", close_percent=100)


def entry_with_take_profit(tp):
    return Decision(
        cycle_id=uuid4(), decision=DecisionAction.LONG, symbol="BTC",
        order=OrderInstruction(type=OrderType.MARKET, entry=65000, size=0.01),
        stop_loss=64000, risk_percent=0.5, take_profit=tp,
        valid_until=until(), reason="test",
    )


def test_scalar_take_profit_coerced_to_single_full_target():
    decision = entry_with_take_profit(65420)
    assert len(decision.take_profit) == 1
    assert decision.take_profit[0].price == 65420
    assert decision.take_profit[0].close_percent == 100.0


def test_target_dict_without_close_percent_defaults_to_full():
    decision = entry_with_take_profit([{"price": 65420}])
    assert decision.take_profit[0].price == 65420
    assert decision.take_profit[0].close_percent == 100.0


def test_multi_scalar_take_profit_ambiguous_and_rejected():
    with pytest.raises(ValidationError, match="close_percent total"):
        entry_with_take_profit([65245, 65420])


def test_multi_target_dict_form_accepted_when_percent_sums_to_100():
    decision = entry_with_take_profit([{"price": 65245, "close_percent": 50}, {"price": 65420, "close_percent": 50}])
    assert [t.price for t in decision.take_profit] == [65245.0, 65420.0]
    assert sum(t.close_percent for t in decision.take_profit) == 100.0


def test_explicit_targets_rejected_when_percent_sums_over_100():
    with pytest.raises(ValidationError, match="close_percent total"):
        entry_with_take_profit([{"price": 65245, "close_percent": 60}, {"price": 65420, "close_percent": 60}])
