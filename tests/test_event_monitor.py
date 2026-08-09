from gpttradder.broker.simulated import SimulatedBroker
from gpttradder.config import Settings
from gpttradder.db import Database
from gpttradder.event_monitor import MarketEventMonitor


class RecordingOrchestrator:
    def __init__(self, db):
        self.db = db
        self.calls = []

    async def run_cycle(self, trigger="POLL", reason=None):
        self.calls.append((trigger, reason))
        return None


async def test_event_monitor_can_interrupt_between_poll_cycles(tmp_path):
    broker = SimulatedBroker()
    settings = Settings(
        db_path=tmp_path / "events.sqlite3",
        candle_limit=30,
        event_move_bps=0.01,
        event_debounce_seconds=5,
    )
    orchestrator = RecordingOrchestrator(Database(settings.db_path))
    monitor = MarketEventMonitor(broker, orchestrator, settings)
    await monitor.scan_once()
    await monitor.scan_once()
    assert orchestrator.calls
    assert all(call[0] == "EVENT" for call in orchestrator.calls)


async def test_simultaneous_events_across_two_symbols_are_all_delivered(tmp_path):
    broker = SimulatedBroker()
    settings = Settings(
        db_path=tmp_path / "events.sqlite3",
        candle_limit=30,
        event_move_bps=0.01,
        event_debounce_seconds=5,
    )
    orchestrator = RecordingOrchestrator(Database(settings.db_path))
    monitor = MarketEventMonitor(broker, orchestrator, settings)
    await monitor.scan_once()
    broker.prices["BTCUSD"] = broker.prices["BTCUSD"] * 1.01
    broker.prices["XAUUSD"] = broker.prices["XAUUSD"] * 1.01
    await monitor.scan_once()
    moved = {
        reason.split(":")[1]
        for _, reason in orchestrator.calls
        if reason.startswith("PRICE_MOVE:")
    }
    assert {"BTC", "XAU"} <= moved
