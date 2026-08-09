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
    Side,
    Position,
    SymbolContractSpec,
    SymbolMarketData,
)
from gpttradder.safety import SafetyEngine


def contract(**overrides):
    data = dict(
        broker_symbol="BTCUSD",
        canonical_symbol="BTC",
        is_tradable=True,
        digits=2,
        point=0.01,
        trade_tick_size=0.01,
        trade_tick_value=0.01,
        trade_contract_size=1.0,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        trade_stops_level=0,
        trade_freeze_level=0,
        trade_mode=4,
        trade_mode_label="FULL",
        currency_base="BTC",
        currency_profit="USD",
        currency_margin="USD",
    )
    data.update(overrides)
    return SymbolContractSpec(**data)


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
                contract=contract(),
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
        account_mode="HEDGING",
        hedging_allowed=True,
        daily_pnl=0,
        daily_start_equity=10_000,
        peak_equity=10_000,
        trailing_drawdown_pct=0,
    )
    data.update(overrides)
    return AccountState(**data)


def long_decision(p: MarketPacket, **overrides):
    data = dict(
        cycle_id=p.cycle_id,
        decision=DecisionAction.LONG,
        symbol="BTCUSD",
        order=OrderInstruction(type=OrderType.MARKET, entry=65000, acceptable_price_range=(64900, 65100), size=0.01),
        stop_loss=64500,
        risk_percent=0.5,
        valid_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        reason="test",
    )
    data.update(overrides)
    return Decision(**data)


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
    d = long_decision(
        p,
        order=OrderInstruction(type=OrderType.MARKET, entry=65000, acceptable_price_range=(64000, 64900), size=0.01),
    )
    result = engine.decision_gate(p, d)
    assert not result.allowed
    assert result.code == "PRICE_OUTSIDE_RANGE"


def test_fresh_price_outside_range_is_rejected():
    engine = SafetyEngine(Settings())
    p = packet(account())
    d = long_decision(p)
    fresh = Quote(bid=65200, ask=65210, spread=10, ts=datetime.now(timezone.utc))
    result = engine.execution_price_gate(d, fresh)
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


def test_position_size_cannot_exceed_declared_risk_budget():
    engine = SafetyEngine(Settings())
    p = packet(account())
    d = long_decision(p, risk_percent=0.5)
    result = engine.risk_budget_gate(p.account, d, decision_risk_amount=80, open_risk_amount=0)
    assert not result.allowed
    assert result.code == "SIZE_EXCEEDS_RISK"


def test_total_open_risk_cannot_exceed_remaining_daily_headroom():
    engine = SafetyEngine(Settings())
    p = packet(account(daily_pnl=-200, equity=9800))
    d = long_decision(p, risk_percent=0.5)
    result = engine.risk_budget_gate(p.account, d, decision_risk_amount=40, open_risk_amount=70)
    assert not result.allowed
    assert result.code == "TOTAL_RISK_EXCEEDS_DAILY_HEADROOM"


def test_invalid_long_stop_geometry_is_rejected():
    engine = SafetyEngine(Settings())
    p = packet(account())
    d = long_decision(p, stop_loss=65100)
    result = engine.decision_gate(p, d)
    assert not result.allowed
    assert result.code == "INVALID_STOP_GEOMETRY"


def test_protective_management_is_not_blocked_by_daily_loss_lock():
    from gpttradder.models import ManagementAction, ManagementInstruction, Position, Side

    engine = SafetyEngine(Settings())
    p = packet(account(equity=9700, balance=9700, daily_pnl=-300))
    p.positions = [
        Position(
            position_id="42", symbol="BTCUSD", side=Side.LONG, size=0.01,
            entry_price=65000, current_price=64900, stop_loss=64500,
            opened_at=datetime.now(timezone.utc),
        )
    ]
    d = Decision(
        cycle_id=p.cycle_id,
        decision=DecisionAction.MANAGE_POSITION,
        symbol="BTCUSD",
        management=ManagementInstruction(action=ManagementAction.BREAK_EVEN, position_id="42"),
        valid_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        reason="protect",
    )
    result = engine.decision_gate(p, d)
    assert result.allowed


def test_new_entry_is_blocked_by_daily_loss_lock():
    engine = SafetyEngine(Settings())
    p = packet(account(equity=9700, balance=9700, daily_pnl=-300))
    d = long_decision(p)
    result = engine.decision_gate(p, d)
    assert not result.allowed
    assert result.code == "DAILY_LOSS_LOCK"


def test_selected_symbol_stale_quote_is_rejected_even_if_packet_timestamp_is_fresh():
    engine = SafetyEngine(Settings(packet_max_age_seconds=30))
    p = packet(account())
    p.broker_timestamp = datetime.now(timezone.utc)
    p.symbols["BTCUSD"].quote.ts = datetime.now(timezone.utc) - timedelta(minutes=2)
    d = long_decision(p)
    result = engine.decision_gate(p, d)
    assert not result.allowed
    assert result.code == "STALE_SYMBOL_QUOTE"


def test_closed_symbol_blocks_new_entry():
    engine = SafetyEngine(Settings())
    p = packet(account())
    p.symbols["BTCUSD"].market["trade_mode_label"] = "CLOSEONLY"
    p.symbols["BTCUSD"].contract = contract(
        trade_mode=3, trade_mode_label="CLOSEONLY", is_tradable=False
    )
    d = long_decision(p)
    result = engine.decision_gate(p, d)
    assert not result.allowed
    assert result.code == "SYMBOL_NOT_OPEN_FOR_ENTRY"


def test_entry_requires_complete_contract_metadata():
    engine = SafetyEngine(Settings())
    p = packet(account())
    p.symbols["BTCUSD"].contract = None
    d = long_decision(p)
    result = engine.decision_gate(p, d)
    assert not result.allowed
    assert result.code == "CONTRACT_METADATA_MISSING"


def test_entry_is_blocked_when_contract_lacks_sizing_fields():
    engine = SafetyEngine(Settings())
    p = packet(account())
    p.symbols["BTCUSD"].contract = contract(volume_min=None, volume_step=None)
    d = long_decision(p)
    result = engine.decision_gate(p, d)
    assert not result.allowed
    assert result.code == "CONTRACT_METADATA_MISSING"


def test_volume_below_minimum_is_rejected():
    engine = SafetyEngine(Settings())
    p = packet(account())
    d = long_decision(p, order=OrderInstruction(type=OrderType.MARKET, entry=65000, size=0.001))
    result = engine.decision_gate(p, d)
    assert not result.allowed
    assert result.code == "VOLUME_BELOW_MINIMUM"


def test_material_volume_normalization_drift_is_rejected():
    engine = SafetyEngine(Settings())
    p = packet(account())
    # 0.017 normalizes down to 0.01 on the 0.01 grid (~41% drift), which would
    # materially change ChatGPT's risk intent -> reject with NORMALIZATION_DRIFT.
    d = long_decision(p, order=OrderInstruction(type=OrderType.MARKET, entry=65000, size=0.017))
    result = engine.decision_gate(p, d)
    assert not result.allowed
    assert result.code == "NORMALIZATION_DRIFT"


def test_netting_account_blocks_second_independent_same_symbol_entry():
    engine = SafetyEngine(Settings())
    p = packet(account(account_mode="NETTING", hedging_allowed=False))
    p.positions = [
        Position(
            position_id="42", symbol="BTCUSD", side=Side.LONG, size=0.01,
            entry_price=64000, current_price=65000, stop_loss=63500,
            opened_at=datetime.now(timezone.utc),
        )
    ]
    d = long_decision(p)
    result = engine.decision_gate(p, d)
    assert not result.allowed
    assert result.code == "NETTING_POSITION_CONFLICT"


def test_hedging_account_allows_independent_same_symbol_entry():
    engine = SafetyEngine(Settings())
    p = packet(account(account_mode="HEDGING", hedging_allowed=True))
    p.positions = [
        Position(
            position_id="42", symbol="BTCUSD", side=Side.SHORT, size=0.01,
            entry_price=65500, current_price=65000, stop_loss=66000,
            opened_at=datetime.now(timezone.utc),
        )
    ]
    d = long_decision(p)
    result = engine.decision_gate(p, d)
    assert result.allowed


def test_stale_fresh_execution_quote_is_rejected():
    engine = SafetyEngine(Settings(packet_max_age_seconds=30))
    p = packet(account())
    d = long_decision(p)
    stale = Quote(
        bid=64990,
        ask=65000,
        spread=10,
        ts=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    result = engine.execution_price_gate(d, stale)
    assert not result.allowed
    assert result.code == "STALE_EXECUTION_QUOTE"


def test_zero_quote_blocks_entry_decision():
    engine = SafetyEngine(Settings())
    p = packet(account())
    p.symbols["BTCUSD"].quote = Quote(bid=0, ask=0, spread=0, ts=datetime.now(timezone.utc))
    d = long_decision(p)
    result = engine.decision_gate(p, d)
    assert not result.allowed
    assert result.code == "INVALID_QUOTE"


def test_zero_quote_blocks_geometry_of_short_entry():
    engine = SafetyEngine(Settings())
    p = packet(account())
    p.symbols["BTCUSD"].quote = Quote(bid=0, ask=0, spread=0, ts=datetime.now(timezone.utc))
    d = Decision(
        cycle_id=p.cycle_id,
        decision=DecisionAction.SHORT,
        symbol="BTCUSD",
        order=OrderInstruction(type=OrderType.MARKET, size=0.01),
        stop_loss=100,
        risk_percent=0.5,
        valid_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        reason="test",
    )
    result = engine.decision_gate(p, d)
    assert not result.allowed
    assert result.code == "INVALID_QUOTE"


def test_inverted_quote_is_rejected_at_execution():
    engine = SafetyEngine(Settings())
    p = packet(account())
    d = long_decision(p)
    inverted = Quote.model_construct(bid=65100, ask=65000, spread=-100, ts=datetime.now(timezone.utc))
    result = engine.execution_price_gate(d, inverted)
    assert not result.allowed
    assert result.code == "INVALID_QUOTE"


def test_protective_close_with_zero_quote_is_blocked():
    engine = SafetyEngine(Settings())
    p = packet(account())
    p.symbols["BTCUSD"].quote = Quote(bid=0, ask=0, spread=0, ts=datetime.now(timezone.utc))
    p.positions = [
        Position(
            position_id="42", symbol="BTCUSD", side=Side.LONG, size=0.01,
            entry_price=64900, current_price=64950, stop_loss=64500,
            opened_at=datetime.now(timezone.utc),
        )
    ]
    d = Decision(
        cycle_id=p.cycle_id,
        decision=DecisionAction.CLOSE_POSITION,
        symbol="BTCUSD",
        position_id="42",
        valid_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        reason="test",
    )
    result = engine.decision_gate(p, d)
    assert not result.allowed
    assert result.code == "INVALID_QUOTE"
