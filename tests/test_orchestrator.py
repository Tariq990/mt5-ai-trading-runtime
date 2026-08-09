from datetime import datetime, timedelta, timezone

import asyncio
import httpx

from gpttradder.collector import MarketCollector
from gpttradder.config import Settings
from gpttradder.db import Database
from gpttradder.decision.base import DecisionProvider
from gpttradder.decision.mock import WaitDecisionProvider
from gpttradder.broker.simulated import SimulatedBroker
from gpttradder.models import Decision, DecisionAction, OrderInstruction, OrderType, Quote
from gpttradder.orchestrator import TradingOrchestrator
from gpttradder.review import EVENT_TRADE_REJECTED, ReviewService
from gpttradder.safety import SafetyEngine


class LongProvider(DecisionProvider):
    def __init__(self, size=0.01, acceptable=(1, 1_000_000)):
        self.size = size
        self.acceptable = acceptable

    async def decide(self, packet):
        quote = packet.symbols["BTC"].quote
        return Decision(
            cycle_id=packet.cycle_id,
            decision=DecisionAction.LONG,
            symbol="BTC",
            order=OrderInstruction(
                type=OrderType.MARKET,
                entry=quote.ask,
                acceptable_price_range=self.acceptable,
                size=self.size,
            ),
            stop_loss=quote.ask - 500,
            risk_percent=0.5,
            valid_until=datetime.now(timezone.utc) + timedelta(minutes=5),
            reason="test",
        )


class JumpingBroker(SimulatedBroker):
    def __init__(self):
        super().__init__()
        self.quote_calls = 0

    async def get_quote(self, symbol: str) -> Quote:
        self.quote_calls += 1
        if self.quote_calls >= 3 and symbol == "BTCUSD":
            self.prices[symbol] = 70_000
        return await super().get_quote(symbol)


def make(settings, broker, provider):
    return TradingOrchestrator(
        broker=broker,
        collector=MarketCollector(broker, settings),
        decisions=provider,
        safety=SafetyEngine(settings),
        db=Database(settings.db_path),
    )


async def test_end_to_end_wait_cycle(tmp_path):
    settings = Settings(db_path=tmp_path / "test.sqlite3", candle_limit=30)
    broker = SimulatedBroker()
    await broker.connect()
    result = await make(settings, broker, WaitDecisionProvider()).run_cycle(reason="test")
    assert result is not None
    assert result.status == "SKIPPED"
    assert result.reason == "WAIT"


async def test_valid_long_executes_once(tmp_path):
    settings = Settings(db_path=tmp_path / "test.sqlite3", candle_limit=30)
    broker = SimulatedBroker()
    result = await make(settings, broker, LongProvider()).run_cycle(reason="entry")
    assert result is not None
    assert result.status == "FILLED"
    assert len(await broker.get_positions()) == 1


async def test_oversized_order_is_rejected_by_monetary_risk(tmp_path):
    settings = Settings(db_path=tmp_path / "test.sqlite3", candle_limit=30)
    broker = SimulatedBroker()
    result = await make(settings, broker, LongProvider(size=1.0)).run_cycle(reason="oversized")
    assert result is not None
    assert result.status == "REJECTED"
    assert "SIZE_EXCEEDS_RISK" in result.reason
    assert await broker.get_positions() == []


async def test_fresh_quote_is_checked_immediately_before_execution(tmp_path):
    settings = Settings(db_path=tmp_path / "test.sqlite3", candle_limit=30)
    broker = JumpingBroker()
    result = await make(settings, broker, LongProvider(acceptable=(64_000, 66_000))).run_cycle(reason="jump")
    assert result is not None
    assert result.status == "REJECTED"
    assert "PRICE_OUTSIDE_RANGE" in result.reason
    assert await broker.get_positions() == []


async def test_rejected_entry_fires_review_event(tmp_path):
    """A rejected LONG/SHORT must produce a TRADE_REJECTED review event with
    the rejection context (quote, contract, risk breakdown)."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True, "send_confirmed": True, "response": "understood"})

    settings = Settings(
        db_path=tmp_path / "test.sqlite3",
        candle_limit=30,
        review_enabled=True,
        review_conversation_url="https://chatgpt.com/c/REVIEW",
        review_retry_delays=[0],
    )
    db = Database(settings.db_path)
    review = ReviewService(settings, db)
    review._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
    broker = SimulatedBroker()
    orchestrator = TradingOrchestrator(
        broker=broker,
        collector=MarketCollector(broker, settings),
        decisions=LongProvider(size=1.0),  # oversized -> monetary-risk rejection
        safety=SafetyEngine(settings),
        db=db,
        review=review,
    )
    await broker.connect()
    result = await orchestrator.run_cycle(reason="review-hook")
    assert result is not None
    assert result.status == "REJECTED"
    await asyncio.gather(*list(review._pending_tasks))

    assert len(calls) == 1
    body = calls[0]
    assert "SIZE_EXCEEDS_RISK" in body
    assert "reviewer" in body.lower()
    rows = db.get_review_events(event_type=EVENT_TRADE_REJECTED)
    assert len(rows) == 1
    assert rows[0]["status"] == "SENT"
