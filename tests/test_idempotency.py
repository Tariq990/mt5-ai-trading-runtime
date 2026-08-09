from datetime import datetime, timedelta, timezone

from gpttradder.db import Database
from gpttradder.models import Decision, DecisionAction, ExecutionResult, MarketPacket, AccountState


def test_decision_and_execution_are_idempotent(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    account = AccountState(
        balance=10000,
        equity=10000,
        free_margin=10000,
        daily_pnl=0,
        peak_equity=10000,
        trailing_drawdown_pct=0,
    )
    packet = MarketPacket(broker_timestamp=datetime.now(timezone.utc), account=account, symbols={})
    db.save_cycle(packet, "hash")
    decision = Decision(
        cycle_id=packet.cycle_id,
        decision=DecisionAction.WAIT,
        valid_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        reason="test",
    )
    assert db.save_decision(decision) is True
    assert db.save_decision(decision) is False

    result = ExecutionResult(
        decision_id=decision.decision_id,
        cycle_id=decision.cycle_id,
        status="SKIPPED",
        reason="WAIT",
    )
    assert db.save_execution(result) is True
    assert db.save_execution(result) is False
    assert db.was_executed(decision.decision_id) is True
