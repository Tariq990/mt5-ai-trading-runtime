from datetime import datetime, timedelta, timezone

from gpttradder.config import Settings
from gpttradder.models import (
    AccountState,
    Decision,
    DecisionAction,
    MarketPacket,
    OrderInstruction,
    OrderType,
    Quote,
    SymbolMarketData,
)
from gpttradder.safety import SafetyEngine


def packet(account: AccountState) -> MarketPacket:
    now = datetime.now(timezone.utc)
    return MarketPacket(
        broker_timestamp=now,
        account=account,
        symbols={
            "BTCUSD": SymbolMarketData(
                symbol="BTCUSD",
                quote=Quote(bid=64990, ask=65000, spread=10, ts=now),
                candles={},
            )
        },
    )


def account(**overrides):
    data = dict(
        account_id="demo",
        is_demo=True,
        balance=10_000,
        equity=10_000,
        free_margin=10_000,
        daily_pnl=0,
        peak_equity=10_000,
        trailing_drawdown_pct=0,
    )
    data.update(overrides)
    return AccountState(**data)


def test_real_account_is_blocked():
    engine = SafetyEngine(Settings())
    result = engine.account_gate(account(is_demo=False))
    assert not result.allowed
    assert result.code == "REAL_ACCOUNT_BLOCKED"


def test_daily_loss_3_percent_is_blocked():
    engine = SafetyEngine(Settings())
    result = engine.account_gate(account(balance=9700, equity=9700, daily_pnl=-300))
    assert not result.allowed
    assert result.code == "DAILY_LOSS_LOCK"


def test_trailing_drawdown_10_percent_is_blocked():
    engine = SafetyEngine(Settings())
    result = engine.account_gate(account(equity=9000, peak_equity=10000, trailing_drawdown_pct=10))
    assert not result.allowed
    assert result.code == "TRAILING_DD_LOCK"


def test_price_outside_range_is_rejected():
    engine = SafetyEngine(Settings())
    p = packet(account())
    d = Decision(
        cycle_id=p.cycle_id,
        decision=DecisionAction.LONG,
        symbol="BTCUSD",
        order=OrderInstruction(type=OrderType.MARKET, entry=65000, acceptable_price_range=(64000, 64900), size=0.01),
        stop_loss=64500,
        valid_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        reason="test",
    )
    result = engine.decision_gate(p, d)
    assert not result.allowed
    assert result.code == "PRICE_OUTSIDE_RANGE"


def test_expired_decision_is_rejected():
    engine = SafetyEngine(Settings())
    p = packet(account())
    d = Decision(
        cycle_id=p.cycle_id,
        decision=DecisionAction.WAIT,
        valid_until=datetime.now(timezone.utc) - timedelta(seconds=1),
        reason="test",
    )
    result = engine.decision_gate(p, d)
    assert not result.allowed
    assert result.code == "DECISION_EXPIRED"
