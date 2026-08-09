from datetime import datetime, timedelta, timezone

from gpttradder.db import Database
from gpttradder.models import Decision, DecisionAction, ExecutionResult, MarketPacket, AccountState


def make_packet():
    account = AccountState(
        balance=10000,
        equity=10000,
        free_margin=10000,
        daily_pnl=0,
        daily_start_equity=10000,
        peak_equity=10000,
        trailing_drawdown_pct=0,
    )
    return MarketPacket(broker_timestamp=datetime.now(timezone.utc), account=account, symbols={})


def make_wait(packet):
    return Decision(
        cycle_id=packet.cycle_id,
        decision=DecisionAction.WAIT,
        valid_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        reason="test",
    )


def test_decision_and_execution_claim_are_idempotent(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    packet = make_packet()
    db.save_cycle(packet, "hash")
    decision = make_wait(packet)
    assert db.save_decision(decision) is True
    assert db.save_decision(decision) is False
    assert db.claim_execution(decision) is True
    assert db.claim_execution(decision) is False

    result = ExecutionResult(
        decision_id=decision.decision_id,
        cycle_id=decision.cycle_id,
        status="SKIPPED",
        reason="WAIT",
    )
    db.finalize_execution(result)
    assert db.was_executed(decision.decision_id) is True
    assert db.get_execution(decision.decision_id)["status"] == "SKIPPED"


def test_cross_process_lock_is_exclusive(tmp_path):
    db1 = Database(tmp_path / "db.sqlite3")
    db2 = Database(tmp_path / "db.sqlite3")
    assert db1.try_acquire_lock("cycle", "a", 30) is True
    assert db2.try_acquire_lock("cycle", "b", 30) is False
    db1.release_lock("cycle", "a")
    assert db2.try_acquire_lock("cycle", "b", 30) is True


def test_risk_state_survives_database_restart(tmp_path):
    path = tmp_path / "db.sqlite3"
    db = Database(path)
    first = db.apply_risk_state(
        AccountState(balance=10000, equity=10000, free_margin=10000, peak_equity=10000, trailing_drawdown_pct=0),
        "Asia/Amman",
    )
    assert first.daily_start_equity == 10000
    db.apply_risk_state(
        AccountState(balance=12000, equity=12000, free_margin=12000, peak_equity=12000, trailing_drawdown_pct=0),
        "Asia/Amman",
    )

    restarted = Database(path)
    after = restarted.apply_risk_state(
        AccountState(balance=10800, equity=10800, free_margin=10800, peak_equity=10800, trailing_drawdown_pct=0),
        "Asia/Amman",
    )
    assert after.peak_equity == 12000
    assert round(after.trailing_drawdown_pct, 6) == 10.0


def test_risk_state_resets_daily_baseline_on_new_local_day_but_keeps_peak(tmp_path):
    path = tmp_path / "db.sqlite3"
    db = Database(path)
    db.set_state("risk_day", "2000-01-01")
    db.set_state("daily_start_equity", 10000)
    db.set_state("peak_equity", 12000)

    state = db.apply_risk_state(
        AccountState(balance=11000, equity=11000, free_margin=11000, peak_equity=11000, trailing_drawdown_pct=0),
        "Asia/Amman",
    )
    assert state.daily_start_equity == 11000
    assert state.daily_pnl == 0
    assert state.peak_equity == 12000
