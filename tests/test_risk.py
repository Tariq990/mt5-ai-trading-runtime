from datetime import datetime, timezone

from gpttradder.models import Position, Side, SymbolContractSpec
from gpttradder.risk import compute_exposure, exposure_notional, normalize_volume


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


def position(symbol, side, size, entry, current=None):
    return Position(
        position_id=f"{symbol}-{side.value}",
        symbol=symbol,
        side=side,
        size=size,
        entry_price=entry,
        current_price=current or entry,
        opened_at=datetime.now(timezone.utc),
    )


def test_normalize_volume_on_step_grid():
    assert normalize_volume(0.05, volume_min=0.01, volume_max=100.0, volume_step=0.01) == 0.05
    assert normalize_volume(0.023, volume_min=0.01, volume_max=100.0, volume_step=0.01) == 0.02
    assert normalize_volume(0.01, volume_min=0.01, volume_max=100.0, volume_step=0.01) == 0.01
    assert normalize_volume(3.14159, volume_min=0.1, volume_max=100.0, volume_step=0.1) == 3.1


def test_normalize_volume_below_minimum_returns_zero():
    assert normalize_volume(0.005, volume_min=0.01, volume_max=100.0, volume_step=0.01) == 0.0
    assert normalize_volume(0.0, volume_min=0.01, volume_max=100.0, volume_step=0.01) == 0.0


def test_normalize_volume_caps_at_maximum():
    assert normalize_volume(500.0, volume_min=0.01, volume_max=100.0, volume_step=0.01) == 100.0


def test_exposure_notional_uses_contract_size():
    crypto_contract = contract(trade_contract_size=1.0)
    fx_contract = contract(
        broker_symbol="EURUSD", canonical_symbol="EURUSD", trade_contract_size=100_000.0,
        currency_base="EUR",
    )
    long_btc = position("BTC", Side.LONG, 1.0, 65_000)
    long_eur = position("EURUSD", Side.LONG, 2.0, 1.08)
    assert exposure_notional(long_btc, crypto_contract) == 65_000.0
    assert exposure_notional(long_eur, fx_contract) == 216_000.0
    assert exposure_notional(long_btc, None) == 65_000.0


def test_aggregate_exposure_context():
    contracts = {
        "BTC": contract(),
        "XAU": contract(broker_symbol="XAUUSD", canonical_symbol="XAU", trade_contract_size=100.0),
    }
    positions = [
        position("BTC", Side.LONG, 1.0, 65_000),
        position("XAU", Side.SHORT, 2.0, 4000),
        position("BTC", Side.SHORT, 0.5, 64_000),
    ]
    ctx = compute_exposure(positions, contracts)
    assert ctx.total_gross_exposure == 65_000 + 800_000 + 32_000
    assert ctx.total_net_exposure == 65_000 - 800_000 - 32_000
    assert ctx.groups["crypto"].position_count == 2
    assert ctx.groups["crypto"].gross_exposure == 65_000 + 32_000
    assert ctx.groups["crypto"].net_exposure == 65_000 - 32_000
    assert ctx.groups["metal"].position_count == 1
    assert ctx.groups["metal"].net_exposure == -800_000
    assert ctx.usd_direction == -65_000 + 800_000 + 32_000


def test_compute_exposure_translates_broker_symbols_via_canonical_of():
    positions = [position("BTCUSDm", Side.LONG, 1.0, 65_000)]
    ctx = compute_exposure(positions, {"BTC": contract()}, canonical_of=lambda s: "BTC" if s == "BTCUSDm" else s)
    assert ctx.groups["crypto"].position_count == 1
    assert ctx.total_gross_exposure == 65_000.0


def test_empty_exposure_context_is_empty():
    ctx = compute_exposure([])
    assert ctx.total_gross_exposure == 0.0
    assert ctx.total_net_exposure == 0.0
    assert ctx.usd_direction == 0.0
    assert all(g.position_count == 0 for g in ctx.groups.values())