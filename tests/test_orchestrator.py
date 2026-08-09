from gpttradder.collector import MarketCollector
from gpttradder.config import Settings
from gpttradder.db import Database
from gpttradder.decision.mock import WaitDecisionProvider
from gpttradder.broker.simulated import SimulatedBroker
from gpttradder.orchestrator import TradingOrchestrator
from gpttradder.safety import SafetyEngine


async def test_end_to_end_wait_cycle(tmp_path):
    settings = Settings(db_path=tmp_path / "test.sqlite3", candle_limit=30)
    broker = SimulatedBroker()
    await broker.connect()
    orchestrator = TradingOrchestrator(
        broker=broker,
        collector=MarketCollector(broker, settings),
        decisions=WaitDecisionProvider(),
        safety=SafetyEngine(settings),
        db=Database(settings.db_path),
    )
    result = await orchestrator.run_cycle(reason="test")
    assert result is not None
    assert result.status == "SKIPPED"
    assert result.reason == "WAIT"
