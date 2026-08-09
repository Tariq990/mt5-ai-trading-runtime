from datetime import datetime, timedelta, timezone
from uuid import uuid4

from gpttradder.broker.simulated import SimulatedBroker
from gpttradder.models import (
    Decision,
    DecisionAction,
    ManagementAction,
    ManagementInstruction,
    OrderInstruction,
    OrderType,
)


def entry(cycle_id, order_type=OrderType.MARKET, entry_price=None):
    return Decision(
        cycle_id=cycle_id,
        decision=DecisionAction.LONG,
        symbol="BTCUSD",
        order=OrderInstruction(type=order_type, entry=entry_price, size=0.01),
        stop_loss=64000,
        risk_percent=0.25,
        valid_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        reason="test",
    )


async def test_market_entry_and_full_close():
    broker = SimulatedBroker()
    await broker.connect()
    d = entry(uuid4())
    opened = await broker.execute(d)
    assert opened.status == "FILLED"
    assert len(await broker.get_positions()) == 1

    close = Decision(
        cycle_id=uuid4(), decision=DecisionAction.CLOSE_POSITION, symbol="BTCUSD",
        position_id=opened.broker_ticket, valid_until=datetime.now(timezone.utc) + timedelta(minutes=5), reason="close",
    )
    result = await broker.execute(close)
    assert result.status == "CLOSED"
    assert await broker.get_positions() == []


async def test_pending_limit_order_is_created():
    broker = SimulatedBroker()
    d = entry(uuid4(), OrderType.LIMIT, 63000)
    result = await broker.execute(d)
    assert result.status == "PENDING"
    pending = await broker.get_pending_orders()
    assert len(pending) == 1
    assert pending[0].order_type == OrderType.LIMIT


async def test_stop_cannot_be_loosened():
    broker = SimulatedBroker()
    opened = await broker.execute(entry(uuid4()))
    manage = Decision(
        cycle_id=uuid4(), decision=DecisionAction.MANAGE_POSITION, symbol="BTCUSD",
        management=ManagementInstruction(
            action=ManagementAction.MOVE_SL,
            position_id=opened.broker_ticket,
            stop_loss=63000,
        ),
        valid_until=datetime.now(timezone.utc) + timedelta(minutes=5), reason="bad move",
    )
    result = await broker.execute(manage)
    assert result.status == "REJECTED"
    assert "loosened" in result.reason
