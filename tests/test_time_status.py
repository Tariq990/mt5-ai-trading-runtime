from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from gpttradder.broker.mt5_demo import MT5DemoBroker
from gpttradder.collector import MarketCollector
from gpttradder.config import Settings
from gpttradder.models import (
    AccountState,
    Candle,
    Decision,
    DecisionAction,
    ManagementAction,
    ManagementInstruction,
    MarketPacket,
    OrderInstruction,
    OrderType,
    Quote,
    SymbolContractSpec,
    SymbolMarketData,
)
from gpttradder.safety import SafetyEngine
from gpttradder.symbols import resolve_canonical_symbols
from gpttradder.timeutil import market_session_open, normalize_broker_epoch, quote_is_fresh

UTC = timezone.utc

# 2026-08-08 is a Saturday; 2026-08-07 is the preceding Friday.
SATURDAY = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
FRIDAY_EVE = datetime(2026, 8, 7, 21, 30, tzinfo=UTC)
SUNDAY_EVE = datetime(2026, 8, 9, 21, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# timeutil: broker-time normalization + freshness (fail closed on the future)
# ---------------------------------------------------------------------------


def test_normalize_broker_epoch_applies_server_offset():
    raw = datetime(2026, 8, 10, 10, 0, tzinfo=UTC).timestamp()  # server clock says 10:00 UTC+0-named
    utc = normalize_broker_epoch(raw, 3.0)
    assert utc == datetime(2026, 8, 10, 7, 0, tzinfo=UTC)
    utc2 = normalize_broker_epoch(raw, 2.0)
    assert utc2 == datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


def test_quote_is_fresh_only_for_valid_window():
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    fresh = now - timedelta(seconds=10)
    assert quote_is_fresh(fresh, 90, 5, now=now)
    stale = now - timedelta(minutes=5)
    assert not quote_is_fresh(stale, 90, 5, now=now)
    future = now + timedelta(hours=3)
    assert not quote_is_fresh(future, 90, 5, now=now)
    slight_skew = now + timedelta(seconds=2)
    assert quote_is_fresh(slight_skew, 90, 5, now=now)


def test_market_session_crypto_open_247():
    assert market_session_open("BTC", SATURDAY)[0]
    assert market_session_open("ETH", SUNDAY_EVE)[0]


def test_market_session_fx_closed_over_weekend():
    assert not market_session_open("EURUSD", FRIDAY_EVE)[0]
    assert "weekend" in (market_session_open("EURUSD", FRIDAY_EVE)[1] or "")
    assert not market_session_open("XAU", SATURDAY)[0]
    assert not market_session_open("GBPUSD", datetime(2026, 8, 9, 0, 0, tzinfo=UTC))[0]


def test_market_session_fx_open_on_weekdays_and_sunday_night():
    assert market_session_open("EURUSD", datetime(2026, 8, 7, 20, 59, tzinfo=UTC))[0]
    assert market_session_open("EURUSD", datetime(2026, 8, 10, 9, 0, tzinfo=UTC))[0]
    assert market_session_open("XAU", SUNDAY_EVE)[0]


# ---------------------------------------------------------------------------
# Collector: decomposed symbol status (the ChatGPT-observed weekend/stale bug)
# ---------------------------------------------------------------------------


class FakeBroker:
    def __init__(self, quotes, contracts=None, account=None, trade_labels=None):
        self.quotes = quotes
        self.contracts = contracts or {}
        self.trade_labels = trade_labels or {}
        self.account = account or AccountState(
            account_id="demo", is_demo=True, balance=10_000, equity=10_000, free_margin=10_000,
            account_mode="HEDGING", hedging_allowed=True, daily_pnl=0, daily_start_equity=10_000,
            peak_equity=10_000, trailing_drawdown_pct=0,
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
            candles={tf: [Candle(ts=quote.ts, open=1, high=1, low=1, close=1, volume=0)] for tf in timeframes},
            market={"trade_mode_label": self.trade_labels.get(symbol, "FULL")},
            contract=self.contracts.get(symbol),
        )


def fresh_quote(ts):
    return Quote(bid=65000, ask=65010, spread=10, ts=ts)


def full_contract(broker_symbol, canonical, **overrides):
    data = dict(
        broker_symbol=broker_symbol, canonical_symbol=canonical, is_tradable=True,
        digits=2, point=0.01, trade_tick_size=0.01, trade_tick_value=0.01,
        trade_contract_size=1.0, volume_min=0.01, volume_max=100.0, volume_step=0.01,
        trade_stops_level=0, trade_freeze_level=0, trade_mode=4, trade_mode_label="FULL",
        currency_base=canonical, currency_profit="USD", currency_margin="USD",
    )
    data.update(overrides)
    return SymbolContractSpec(**data)


async def test_future_quote_is_time_invalid_and_excluded():
    # The exact failure ChatGPT flagged: quotes stamped ~3h ahead of UTC were
    # treated as perpetually fresh. A future-stamped quote must fail closed.
    now = SATURDAY
    broker = FakeBroker(
        {
            "BTCUSD": fresh_quote(now + timedelta(hours=3)),
            "ETHUSD": fresh_quote(now - timedelta(seconds=10)),
        },
        contracts={"BTCUSD": full_contract("BTCUSD", "BTC"), "ETHUSD": full_contract("ETHUSD", "ETH")},
    )
    settings = Settings(symbols=["BTC", "ETH"])
    packet = await MarketCollector(broker, settings, now_fn=lambda: now).collect()
    assert "ETH" in packet.symbols
    assert "BTC" not in packet.symbols
    st = packet.symbol_status["BTC"]
    assert st.status == "TIME_INVALID"
    assert st.quote_fresh is False
    assert st.executable_now is False
    assert st.quote_age_seconds == pytest.approx(-3 * 3600, abs=5)


async def test_weekend_stale_xau_is_visible_but_not_executable():
    # Weekend XAU: the broker re-emits Friday's quote; it must NOT look tradable.
    broker = FakeBroker(
        {
            "BTCUSD": fresh_quote(SATURDAY - timedelta(seconds=10)),
            "XAUUSD": fresh_quote(FRIDAY_EVE),
        },
        contracts={"BTCUSD": full_contract("BTCUSD", "BTC"), "XAUUSD": full_contract("XAUUSD", "XAU")},
    )
    settings = Settings(symbols=["BTC", "XAU"])
    packet = await MarketCollector(broker, settings, now_fn=lambda: SATURDAY).collect()
    btc = packet.symbol_status["BTC"]
    assert btc.status == "TRADABLE"
    assert btc.quote_fresh is True
    assert btc.market_session_open is True
    assert btc.executable_now is True
    xau = packet.symbol_status["XAU"]
    assert packet.symbols["XAU"].symbol == "XAU"  # visible to ChatGPT...
    assert xau.status == "NOT_TRADABLE"
    assert xau.quote_fresh is False
    assert xau.market_session_open is False
    assert xau.executable_now is False


async def test_stale_weekday_quote_is_not_executable_but_still_visible():
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    broker = FakeBroker(
        {"BTCUSD": fresh_quote(now - timedelta(hours=2))},
        contracts={"BTCUSD": full_contract("BTCUSD", "BTC")},
    )
    settings = Settings(symbols=["BTC"])
    packet = await MarketCollector(broker, settings, now_fn=lambda: now).collect()
    st = packet.symbol_status["BTC"]
    assert st.status == "NOT_TRADABLE"
    assert st.quote_fresh is False
    assert st.market_session_open is True
    assert st.executable_now is False


async def test_closeonly_trade_mode_is_not_executable():
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    broker = FakeBroker(
        {"BTCUSD": fresh_quote(now)},
        contracts={"BTCUSD": full_contract("BTCUSD", "BTC", is_tradable=False, trade_mode=3, trade_mode_label="CLOSEONLY")},
    )
    settings = Settings(symbols=["BTC"])
    packet = await MarketCollector(broker, settings, now_fn=lambda: now).collect()
    st = packet.symbol_status["BTC"]
    assert st.status == "NOT_TRADABLE"
    assert st.broker_trade_mode == "CLOSEONLY"
    assert st.executable_now is False
    assert packet.symbols["BTC"].symbol == "BTC"


# ---------------------------------------------------------------------------
# SafetyEngine: new SYMBOL_NOT_EXECUTABLE gate (+ management stays allowed)
# ---------------------------------------------------------------------------


def packet_for(symbol_status, quote_ts=None, positions=()):
    now = quote_ts or datetime.now(timezone.utc)
    return MarketPacket(
        broker_timestamp=now,
        account=AccountState(
            account_id="demo", is_demo=True, balance=10_000, equity=10_000, free_margin=10_000,
            account_mode="HEDGING", hedging_allowed=True, daily_pnl=0, daily_start_equity=10_000,
            peak_equity=10_000, trailing_drawdown_pct=0,
        ),
        symbols={
            "BTC": SymbolMarketData(
                symbol="BTC", quote=Quote(bid=64990, ask=65000, spread=10, ts=now),
                candles={}, contract=full_contract("BTCUSD", "BTC"),
            )
        },
        positions=list(positions),
        symbol_status={"BTC": symbol_status},
    )


def not_executable_status(reason="market session closed (weekend)"):
    from gpttradder.models import SymbolStatus

    return SymbolStatus(
        canonical="BTC", broker_symbol="BTCUSD", status="NOT_TRADABLE", reason=reason,
        broker_trade_mode="FULL", quote_fresh=False, quote_age_seconds=42.0,
        market_session_open=False, executable_now=False,
    )


def long_decision(p):
    return Decision(
        cycle_id=p.cycle_id,
        decision=DecisionAction.LONG,
        symbol="BTC",
        order=OrderInstruction(type=OrderType.MARKET, entry=65000, acceptable_price_range=(64900, 65100), size=0.01),
        stop_loss=64500,
        risk_percent=0.5,
        valid_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        reason="test",
    )


def test_new_entry_blocked_when_symbol_not_executable():
    engine = SafetyEngine(Settings())
    p = packet_for(not_executable_status())
    result = engine.decision_gate(p, long_decision(p))
    assert not result.allowed
    assert result.code == "SYMBOL_NOT_EXECUTABLE"
    assert "weekend" in result.reason


def test_manage_position_stays_available_when_symbol_not_executable():
    from gpttradder.models import Position, Side

    now = datetime.now(timezone.utc)
    position = Position(
        position_id="42", symbol="BTC", side=Side.LONG, size=0.01,
        entry_price=65000, current_price=65100, stop_loss=64000, opened_at=now,
    )
    p = packet_for(not_executable_status(), positions=[position])
    engine = SafetyEngine(Settings())
    d = Decision(
        cycle_id=p.cycle_id, decision=DecisionAction.MANAGE_POSITION, symbol="BTC",
        management=ManagementInstruction(action=ManagementAction.MOVE_SL, position_id="42", stop_loss=64500),
        valid_until=datetime.now(timezone.utc) + timedelta(minutes=5), reason="tighten",
    )
    result = engine.decision_gate(p, d)
    assert result.allowed
    assert result.code == "OK"


def test_future_quote_fails_stale_symbol_quote_gate():
    now = datetime.now(timezone.utc)
    p = packet_for(not_executable_status(), quote_ts=now + timedelta(hours=3))
    p.symbol_status["BTC"] = p.symbol_status["BTC"].model_copy(update={"executable_now": True})
    engine = SafetyEngine(Settings())
    result = engine.decision_gate(p, long_decision(p))
    assert not result.allowed
    assert result.code == "STALE_SYMBOL_QUOTE"


# ---------------------------------------------------------------------------
# MT5 adapter: server-clock normalization (FakeMT5, no terminal needed)
# ---------------------------------------------------------------------------


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    POSITION_TYPE_BUY = 0
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1

    def account_info(self):
        return SimpleNamespace(login=1, trade_mode=0, balance=1, equity=1, margin_free=1, margin_mode=2)

    def symbol_info(self, symbol):
        return SimpleNamespace(
            visible=True, volume_min=0.01, volume_max=100.0, volume_step=0.01,
            trade_mode=4, point=0.01, trade_stops_level=0, description=symbol, path=symbol,
            digits=2, trade_tick_size=0.01, trade_tick_value=0.01,
            trade_tick_value_profit=0.01, trade_tick_value_loss=0.01, trade_contract_size=1.0,
            volume_limit=0, trade_freeze_level=0, trade_calc_mode=0, filling_mode=1,
            order_mode=1, expiration_mode=0, trade_exemode=0, currency_base="BTC",
            currency_profit="USD", currency_margin="USD", swap_mode=0, swap_long=0.0,
            swap_short=0.0, swap_rollover3days=3, margin_initial=0.0, margin_maintenance=0.0,
        )

    def symbols_get(self):
        return [SimpleNamespace(name="BTCUSD")]

    def symbol_select(self, symbol, visible):
        return True

    def symbol_info_tick(self, symbol):
        server_now = datetime.now(timezone.utc) + timedelta(hours=3)  # broker clock: UTC+3 (EEST)
        return SimpleNamespace(bid=64990.0, ask=65000.0, time=int(server_now.timestamp()), time_msc=int(server_now.timestamp() * 1000))

    def last_error(self):
        return (0, "ok")


async def test_mt5_quote_timestamp_normalized_from_server_clock_to_utc():
    fake = FakeMT5()
    broker = MT5DemoBroker(server_utc_offset_hours=3.0)  # EEST summer offset
    broker.mt5 = fake
    await broker.resolve_symbols(["BTC"], {})
    quote = await broker.get_quote("BTCUSD")
    now = datetime.now(timezone.utc)
    assert abs((now - quote.ts).total_seconds()) < 5  # normalized back to real UTC


async def test_mt5_default_offset_leaves_quote_in_the_future_not_fresh():
    fake = FakeMT5()
    broker = MT5DemoBroker()  # default 2.0 while the server is really +3 -> quote lands 1h ahead
    broker.mt5 = fake
    await broker.resolve_symbols(["BTC"], {})
    quote = await broker.get_quote("BTCUSD")
    age = (datetime.now(timezone.utc) - quote.ts).total_seconds()
    assert -3610 < age < -3590  # future-skewed, so freshness gates will fail closed