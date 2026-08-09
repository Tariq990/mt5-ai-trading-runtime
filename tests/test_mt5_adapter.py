from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from gpttradder.broker.mt5_demo import MT5DemoBroker
from gpttradder.config import Settings
from gpttradder.verification import verify_mt5_demo_write
from gpttradder.models import (
    Decision,
    DecisionAction,
    ManagementAction,
    ManagementInstruction,
    OrderInstruction,
    OrderType,
)


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
    ACCOUNT_MARGIN_MODE_EXCHANGE = 1
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    ORDER_TYPE_BUY_STOP_LIMIT = 6
    ORDER_TYPE_SELL_STOP_LIMIT = 7
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_REMOVE = 8
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    SYMBOL_TRADE_MODE_DISABLED = 0
    SYMBOL_TRADE_MODE_LONGONLY = 1
    SYMBOL_TRADE_MODE_SHORTONLY = 2
    SYMBOL_TRADE_MODE_CLOSEONLY = 3
    SYMBOL_TRADE_MODE_FULL = 4

    def __init__(self, real=False):
        self.real = real
        self.requests = []
        self._positions = []
        self._orders = []

    def account_info(self):
        return SimpleNamespace(
            login=123,
            trade_mode=self.ACCOUNT_TRADE_MODE_REAL if self.real else self.ACCOUNT_TRADE_MODE_DEMO,
            balance=10000.0,
            equity=10000.0,
            margin_free=10000.0,
            margin_mode=self.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
        )

    def symbol_info(self, symbol):
        return SimpleNamespace(
            visible=True, volume_min=0.01, volume_max=100.0, volume_step=0.01,
            trade_mode=self.SYMBOL_TRADE_MODE_FULL, point=0.01, trade_stops_level=0,
            description=symbol, path=symbol, digits=2, trade_tick_size=0.01,
            trade_tick_value=0.01, trade_tick_value_profit=0.01, trade_tick_value_loss=0.01,
            trade_contract_size=1.0, volume_limit=0, trade_freeze_level=0,
            trade_calc_mode=0, filling_mode=1, order_mode=1, expiration_mode=0,
            trade_exemode=0, currency_base="BTC", currency_profit="USD",
            currency_margin="USD", swap_mode=0, swap_long=0.0, swap_short=0.0,
            swap_rollover3days=3, margin_initial=0.0, margin_maintenance=0.0,
        )

    def symbols_get(self):
        return [
            SimpleNamespace(name="BTCUSD"),
            SimpleNamespace(name="ETHUSD"),
            SimpleNamespace(name="XAUUSD"),
            SimpleNamespace(name="EURUSD"),
            SimpleNamespace(name="GBPUSD"),
        ]

    def symbol_select(self, symbol, visible):
        return True

    def symbol_info_tick(self, symbol):
        now = datetime.now(timezone.utc)
        return SimpleNamespace(bid=64990.0, ask=65000.0, time=int(now.timestamp()), time_msc=int(now.timestamp() * 1000))

    def order_check(self, request):
        return SimpleNamespace(retcode=0, comment="ok")

    def order_send(self, request):
        self.requests.append(dict(request))
        if request["action"] == self.TRADE_ACTION_PENDING:
            ticket = 777
            self._orders = [SimpleNamespace(
                ticket=ticket, symbol=request["symbol"], type=request["type"],
                volume_current=request["volume"], volume_initial=request["volume"],
                price_open=request["price"], sl=request.get("sl", 0), tp=request.get("tp", 0),
                time_setup=int(datetime.now(timezone.utc).timestamp()),
            )]
            return SimpleNamespace(retcode=self.TRADE_RETCODE_PLACED, order=ticket, deal=0, price=0, volume=request["volume"], comment="placed")
        if request["action"] == self.TRADE_ACTION_REMOVE:
            self._orders = [o for o in self._orders if int(o.ticket) != int(request["order"])]
            return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=request["order"], deal=0, price=0, volume=0, comment="removed")
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=888, deal=999, price=request.get("price", 65000), volume=request.get("volume", 0.01), comment="done")

    def positions_get(self, ticket=None):
        if ticket is None:
            return tuple(self._positions)
        return tuple(p for p in self._positions if int(p.ticket) == int(ticket))

    def orders_get(self, ticket=None):
        if ticket is None:
            return tuple(self._orders)
        return tuple(o for o in self._orders if int(o.ticket) == int(ticket))

    def history_deals_get(self, start, end):
        return ()

    def order_calc_profit(self, action, symbol, volume, price_open, price_close):
        return (price_close - price_open) * volume * (1 if action == self.ORDER_TYPE_BUY else -1)

    def last_error(self):
        return (0, "ok")


def future():
    return datetime.now(timezone.utc) + timedelta(minutes=5)


async def ready_broker(fake):
    broker = MT5DemoBroker()
    broker.mt5 = fake
    await broker.resolve_symbols(["BTC"], {})
    return broker


def trade(order_type=OrderType.MARKET, entry=None):
    return Decision(
        cycle_id=uuid4(), decision=DecisionAction.LONG, symbol="BTC",
        order=OrderInstruction(type=order_type, entry=entry, size=0.02),
        stop_loss=64000, risk_percent=0.25, valid_until=future(), reason="test",
    )


async def test_mt5_account_state_reports_hedging_capability():
    fake = FakeMT5()
    broker = await ready_broker(fake)
    state = await broker.get_account_state()
    assert state.account_mode == "HEDGING"
    assert state.hedging_allowed is True


async def test_mt5_real_account_guard_blocks_everything():
    broker = MT5DemoBroker()
    broker.mt5 = FakeMT5(real=True)
    with pytest.raises(RuntimeError, match="REAL ACCOUNT BLOCKED"):
        await broker.assert_demo()


async def test_mt5_market_entry_uses_deal_action():
    fake = FakeMT5()
    broker = await ready_broker(fake)
    result = await broker.execute(trade())
    assert result.status == "FILLED"
    assert fake.requests[-1]["action"] == fake.TRADE_ACTION_DEAL
    assert fake.requests[-1]["type"] == fake.ORDER_TYPE_BUY


async def test_mt5_limit_entry_uses_pending_order():
    fake = FakeMT5()
    broker = await ready_broker(fake)
    result = await broker.execute(trade(OrderType.LIMIT, 64000))
    assert result.status == "PENDING"
    assert fake.requests[-1]["action"] == fake.TRADE_ACTION_PENDING
    assert fake.requests[-1]["type"] == fake.ORDER_TYPE_BUY_LIMIT
    assert fake.requests[-1]["price"] == 64000


async def test_mt5_manage_move_sl_uses_sltp_action():
    fake = FakeMT5()
    fake._positions = [
        SimpleNamespace(
            ticket=42, symbol="BTCUSD", type=fake.POSITION_TYPE_BUY, volume=0.02,
            price_open=65000.0, price_current=65100.0, sl=64000.0, tp=66000.0,
            profit=2.0, time=int(datetime.now(timezone.utc).timestamp()),
        )
    ]
    broker = await ready_broker(fake)
    d = Decision(
        cycle_id=uuid4(), decision=DecisionAction.MANAGE_POSITION, symbol="BTC",
        management=ManagementInstruction(action=ManagementAction.MOVE_SL, position_id="42", stop_loss=64500),
        valid_until=future(), reason="tighten",
    )
    result = await broker.execute(d)
    assert result.status == "MODIFIED"
    assert fake.requests[-1]["action"] == fake.TRADE_ACTION_SLTP
    assert fake.requests[-1]["sl"] == 64500


async def test_mt5_monetary_sl_risk_uses_broker_calc():
    fake = FakeMT5()
    broker = await ready_broker(fake)
    risk = await broker.estimate_decision_risk(trade())
    # LONG MARKET 0.02 with fresh ask 65000, SL 64000 -> (65000-64000)*0.02 = 20.
    assert risk == pytest.approx(20.0)


async def test_mt5_demo_write_verifier_places_and_cancels_pending_order():
    fake = FakeMT5()
    broker = await ready_broker(fake)
    result = await verify_mt5_demo_write(Settings(symbols=["BTC"]), broker)
    assert result["ok"] is True
    assert result["placement_status"] == "PENDING"
    assert result["cancel_status"] == "CANCELLED"
    actions = [r["action"] for r in fake.requests]
    assert fake.TRADE_ACTION_PENDING in actions
    assert fake.TRADE_ACTION_REMOVE in actions
    assert fake._orders == []
