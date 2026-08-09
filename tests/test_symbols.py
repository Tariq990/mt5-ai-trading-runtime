from gpttradder.symbols import CANONICAL_SYMBOLS, EXPOSURE_GROUPS, exposure_group_of, resolve_canonical_symbols


def test_btc_symbol_resolution():
    result = resolve_canonical_symbols(["BTCUSD"], ["BTC"])["BTC"]
    assert result.status == "OK"
    assert result.broker_symbol == "BTCUSD"


def test_eth_symbol_resolution():
    result = resolve_canonical_symbols(["ETHUSD"], ["ETH"])["ETH"]
    assert result.status == "OK"
    assert result.broker_symbol == "ETHUSD"


def test_xau_symbol_resolution():
    result = resolve_canonical_symbols(["XAUUSD.a"], ["XAU"])["XAU"]
    assert result.status == "OK"
    assert result.broker_symbol == "XAUUSD.A"


def test_eurusd_symbol_resolution():
    result = resolve_canonical_symbols(["EURUSD"], ["EURUSD"])["EURUSD"]
    assert result.status == "OK"
    assert result.broker_symbol == "EURUSD"


def test_gbpusd_symbol_resolution():
    result = resolve_canonical_symbols(["GBPUSD"], ["GBPUSD"])["GBPUSD"]
    assert result.status == "OK"
    assert result.broker_symbol == "GBPUSD"


def test_broker_suffix_discovery():
    results = resolve_canonical_symbols(
        ["BTCUSDm", "ETHUSDm", "XAUUSD.a", "EURUSD.", "GBPUSD-i"],
        list(CANONICAL_SYMBOLS),
    )
    assert results["BTC"].status == "OK" and results["BTC"].broker_symbol == "BTCUSDM"
    assert results["ETH"].status == "OK" and results["ETH"].broker_symbol == "ETHUSDM"
    assert results["XAU"].status == "OK" and results["XAU"].broker_symbol == "XAUUSD.A"
    assert results["EURUSD"].status == "OK" and results["EURUSD"].broker_symbol == "EURUSD."
    assert results["GBPUSD"].status == "OK" and results["GBPUSD"].broker_symbol == "GBPUSD-I"


def test_suffix_discovery_rejects_unusual_suffixes():
    results = resolve_canonical_symbols(["XAUUSD/a"], ["XAU"])
    assert results["XAU"].status == "NOT_FOUND"


def test_ambiguous_symbol_fails_closed():
    results = resolve_canonical_symbols(["BTCUSD", "BTCUSDm"], ["BTC"])
    assert results["BTC"].status == "AMBIGUOUS"
    assert results["BTC"].broker_symbol is None
    assert len(results["BTC"].candidates) == 2


def test_ambiguous_symbol_resolved_via_symbol_map():
    results = resolve_canonical_symbols(["BTCUSD", "BTCUSDm"], ["BTC"], symbol_map={"BTC": "BTCUSDm"})
    assert results["BTC"].status == "OK"
    assert results["BTC"].broker_symbol == "BTCUSDM"
    assert results["BTC"].reason == "explicit symbol_map override"


def test_missing_symbol_fails_closed():
    results = resolve_canonical_symbols(["BTCUSD", "XAUUSD"], ["ETH"])
    assert results["ETH"].status == "NOT_FOUND"
    assert results["ETH"].broker_symbol is None


def test_symbol_map_override_missing_at_broker_fails_closed():
    results = resolve_canonical_symbols(["BTCUSD"], ["BTC"], symbol_map={"BTC": "BTCUSDm"})
    assert results["BTC"].status == "NOT_FOUND"
    assert "not available" in results["BTC"].reason


def test_real_broker_stock_ticker_collision_is_ambiguous_without_map():
    # This broker (7421 symbols) carries BTCT.NAS-24, ETHA.NAS-24 and six XAU
    # crosses, so BTC/ETH/XAU must fail closed until an explicit map is set.
    available = ["BTCUSD", "BTCT.NAS-24", "ETHUSD", "ETHA.NAS-24",
                 "XAUAUD", "XAUCHF", "XAUEUR", "XAUGBP", "XAUJPY", "XAUUSD",
                 "EURUSD", "GBPUSD"]
    results = resolve_canonical_symbols(available, list(CANONICAL_SYMBOLS))
    assert results["BTC"].status == "AMBIGUOUS"
    assert results["ETH"].status == "AMBIGUOUS"
    assert results["XAU"].status == "AMBIGUOUS"
    assert results["EURUSD"].status == "OK"
    assert results["GBPUSD"].status == "OK"


def test_real_broker_stock_ticker_collision_resolved_via_map():
    available = ["BTCUSD", "BTCT.NAS-24", "ETHUSD", "ETHA.NAS-24",
                 "XAUAUD", "XAUCHF", "XAUEUR", "XAUGBP", "XAUJPY", "XAUUSD",
                 "EURUSD", "GBPUSD"]
    symbol_map = {"BTC": "BTCUSD", "ETH": "ETHUSD", "XAU": "XAUUSD"}
    results = resolve_canonical_symbols(available, list(CANONICAL_SYMBOLS), symbol_map=symbol_map)
    assert results["BTC"].broker_symbol == "BTCUSD"
    assert results["ETH"].broker_symbol == "ETHUSD"
    assert results["XAU"].broker_symbol == "XAUUSD"
    assert all(r.status == "OK" for r in results.values())


def test_canonical_symbols_and_exposure_groups():
    assert list(CANONICAL_SYMBOLS) == ["BTC", "ETH", "XAU", "EURUSD", "GBPUSD"]
    assert EXPOSURE_GROUPS["crypto"] == frozenset({"BTC", "ETH"})
    assert EXPOSURE_GROUPS["usd_fx"] == frozenset({"EURUSD", "GBPUSD"})
    assert EXPOSURE_GROUPS["metal"] == frozenset({"XAU"})
    assert exposure_group_of("BTC") == "crypto"
    assert exposure_group_of("EURUSD") == "usd_fx"
    assert exposure_group_of("XAU") == "metal"
    assert exposure_group_of("SOL") is None