from datetime import datetime, timezone

import pytest

from gpttradder.collector import MarketCollector
from gpttradder.config import Settings
from gpttradder.models import AccountState, Candle, Quote, SymbolContractSpec, SymbolMarketData
from gpttradder.symbols import resolve_canonical_symbols


class FakeBroker:
    def __init__(self, quotes, account=None, contracts=None):
        self.quotes = quotes
        self.contracts = contracts or {}
        self.account = account or AccountState(
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
        self._resolution = {}

    async def resolve_symbols(self, canonical, symbol_map):
        self._resolution = resolve_canonical_symbols(self.quotes.keys(), canonical=canonical, symbol_map=symbol_map)
        return self._resolution

    def broker_symbol_to_canonical(self):
        return {r.broker_symbol: r.canonical for r in self._resolution.values() if r.broker_symbol}

    def to_broker_symbol(self, canonical):
        return self._resolution[canonical].broker_symbol

    async def get_account_state(self):
        return self.account

    async def get_positions(self):
        return []

    async def get_pending_orders(self):
        return []

    async def get_recent_trades(self):
        return []

    async def get_quote(self, symbol):
        return self.quotes[symbol]

    async def get_market_data(self, symbol, timeframes, limit):
        quote = self.quotes[symbol]
        return SymbolMarketData.model_construct(
            symbol=symbol,
            quote=quote,
            candles={
                tf: [Candle(ts=datetime.now(timezone.utc), open=1, high=1, low=1, close=1, volume=0)]
                for tf in timeframes
            },
            market={},
            contract=self.contracts.get(symbol),
        )


async def test_collector_skips_symbol_with_zero_quote():
    now = datetime.now(timezone.utc)
    broker = FakeBroker(
        {
            "BTCUSD": Quote(bid=0, ask=0, spread=0, ts=now),
            "XAUUSD": Quote(bid=4340, ask=4341, spread=1, ts=now),
        }
    )
    settings = Settings(symbols=["BTC", "XAU"])
    packet = await MarketCollector(broker, settings).collect()
    assert list(packet.symbols.keys()) == ["XAU"]
    assert packet.symbol_status["XAU"].status == "TRADABLE"
    assert packet.symbol_status["BTC"].status == "NO_QUOTE"
    assert packet.symbols["XAU"].symbol == "XAU"


async def test_collector_skips_symbol_with_inverted_quote():
    now = datetime.now(timezone.utc)
    inverted = Quote.model_construct(bid=65000, ask=64900, spread=-100, ts=now)
    broker = FakeBroker(
        {
            "BTCUSD": inverted,
            "XAUUSD": Quote(bid=4340, ask=4341, spread=1, ts=now),
        }
    )
    settings = Settings(symbols=["BTC", "XAU"])
    packet = await MarketCollector(broker, settings).collect()
    assert list(packet.symbols.keys()) == ["XAU"]
    assert packet.symbol_status["BTC"].status == "NO_QUOTE"


async def test_collector_raises_when_all_symbols_closed():
    now = datetime.now(timezone.utc)
    broker = FakeBroker(
        {
            "BTCUSD": Quote(bid=0, ask=0, spread=0, ts=now),
            "XAUUSD": Quote(bid=0, ask=0, spread=0, ts=now),
        }
    )
    settings = Settings(symbols=["BTC", "XAU"])
    with pytest.raises(RuntimeError):
        await MarketCollector(broker, settings).collect()


async def test_collector_reports_unresolved_canonical_symbol():
    now = datetime.now(timezone.utc)
    broker = FakeBroker(
        {
            "BTCUSD": Quote(bid=65000, ask=65010, spread=10, ts=now),
        }
    )
    settings = Settings(symbols=["BTC", "ETH"])
    packet = await MarketCollector(broker, settings).collect()
    assert list(packet.symbols.keys()) == ["BTC"]
    assert packet.symbol_status["ETH"].status == "UNRESOLVED"
    assert packet.symbol_status["BTC"].status == "TRADABLE"


def full_contract(broker_symbol, canonical):
    return SymbolContractSpec(
        broker_symbol=broker_symbol,
        canonical_symbol=canonical,
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
        currency_base=canonical,
        currency_profit="USD",
        currency_margin="USD",
    )


def five_quotes():
    now = datetime.now(timezone.utc)
    names = ["BTCUSD", "ETHUSD", "XAUUSD", "EURUSD", "GBPUSD"]
    return {name: Quote(bid=1_000 * (i + 1), ask=1_000 * (i + 1) + 10, spread=10, ts=now) for i, name in enumerate(names)}


async def test_collector_builds_packet_with_all_five_instruments():
    quotes = five_quotes()
    contracts = {
        "BTCUSD": full_contract("BTCUSD", "BTC"),
        "ETHUSD": full_contract("ETHUSD", "ETH"),
        "XAUUSD": full_contract("XAUUSD", "XAU"),
        "EURUSD": full_contract("EURUSD", "EURUSD"),
        "GBPUSD": full_contract("GBPUSD", "GBPUSD"),
    }
    broker = FakeBroker(quotes, contracts=contracts)
    packet = await MarketCollector(broker, Settings()).collect()
    assert list(packet.symbols.keys()) == ["BTC", "ETH", "XAU", "EURUSD", "GBPUSD"]
    assert all(packet.symbol_status[s].status == "TRADABLE" for s in packet.symbols)
    assert packet.symbols["BTC"].contract is not None
    assert packet.symbols["BTC"].contract.broker_symbol == "BTCUSD"
    assert packet.exposure is not None
    assert set(packet.exposure.groups) == {"crypto", "usd_fx", "metal"}


async def test_closed_xau_does_not_invalidate_healthy_btc_packet():
    now = datetime.now(timezone.utc)
    quotes = {
        "BTCUSD": Quote(bid=65000, ask=65010, spread=10, ts=now),
        "XAUUSD": Quote(bid=0, ask=0, spread=0, ts=now),
    }
    broker = FakeBroker(quotes)
    settings = Settings(symbols=["BTC", "XAU"])
    packet = await MarketCollector(broker, settings).collect()
    assert list(packet.symbols.keys()) == ["BTC"]
    assert packet.symbol_status["BTC"].status == "TRADABLE"
    assert packet.symbol_status["XAU"].status == "NO_QUOTE"
    assert packet.broker_timestamp == packet.symbols["BTC"].quote.ts


async def test_stale_eth_surfaces_as_no_quote_while_btc_stays_healthy():
    now = datetime.now(timezone.utc)
    quotes = {
        "BTCUSD": Quote(bid=65000, ask=65010, spread=10, ts=now),
        "ETHUSD": Quote(bid=0, ask=0, spread=0, ts=now),
    }
    broker = FakeBroker(quotes)
    settings = Settings(symbols=["BTC", "ETH"])
    packet = await MarketCollector(broker, settings).collect()
    assert list(packet.symbols.keys()) == ["BTC"]
    assert packet.symbol_status["BTC"].status == "TRADABLE"
    assert packet.symbol_status["ETH"].status == "NO_QUOTE"