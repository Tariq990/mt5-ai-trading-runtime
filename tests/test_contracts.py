from gpttradder.models import SymbolContractSpec


def test_contract_spec_parses_complete_metadata():
    spec = SymbolContractSpec(
        broker_symbol="BTCUSDm",
        canonical_symbol="BTC",
        is_tradable=True,
        description="Bitcoin vs US Dollar",
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
        swap_mode=0,
        swap_long=-2.0,
        swap_short=1.5,
        swap_rollover3days=3,
        margin_initial=0.0,
        margin_maintenance=0.0,
    )
    assert spec.broker_symbol == "BTCUSDm"
    assert spec.canonical_symbol == "BTC"
    assert spec.is_tradable is True
    assert spec.description == "Bitcoin vs US Dollar"


def test_contract_size_and_tick_metadata():
    spec = SymbolContractSpec(
        broker_symbol="EURUSD",
        canonical_symbol="EURUSD",
        trade_contract_size=100_000.0,
        trade_tick_size=0.00001,
        trade_tick_value=1.0,
    )
    assert spec.trade_contract_size == 100_000.0
    assert spec.trade_tick_size == 0.00001
    assert spec.trade_tick_value == 1.0


def test_contract_volume_limits():
    spec = SymbolContractSpec(
        broker_symbol="XAUUSD.a",
        canonical_symbol="XAU",
        volume_min=0.1,
        volume_max=50.0,
        volume_step=0.1,
    )
    assert spec.volume_min == 0.1
    assert spec.volume_max == 50.0
    assert spec.volume_step == 0.1


def test_contract_stops_and_freeze_levels():
    spec = SymbolContractSpec(
        broker_symbol="XAUUSD.a",
        canonical_symbol="XAU",
        trade_stops_level=20,
        trade_freeze_level=10,
    )
    assert spec.trade_stops_level == 20
    assert spec.trade_freeze_level == 10


def test_contract_currency_metadata():
    spec = SymbolContractSpec(
        broker_symbol="GBPUSD",
        canonical_symbol="GBPUSD",
        currency_base="GBP",
        currency_profit="USD",
        currency_margin="USD",
    )
    assert spec.currency_base == "GBP"
    assert spec.currency_profit == "USD"
    assert spec.currency_margin == "USD"


def test_contract_defaults_are_safe():
    spec = SymbolContractSpec(broker_symbol="BTCUSD", canonical_symbol="BTC")
    assert spec.is_tradable is True
    assert spec.volume_min is None
    assert spec.trade_contract_size is None
    assert spec.trade_tick_value is None
    assert spec.trade_mode_label is None