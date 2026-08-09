from __future__ import annotations

from .broker.base import Broker
from .broker.mt5_demo import MT5DemoBroker
from .broker.simulated import SimulatedBroker
from .collector import MarketCollector
from .config import Settings
from .db import Database
from .decision.http_bridge import HTTPDecisionBridge
from .decision.mock import WaitDecisionProvider
from .orchestrator import TradingOrchestrator
from .safety import SafetyEngine


def build_broker(settings: Settings) -> Broker:
    if settings.broker == "mt5":
        return MT5DemoBroker()
    return SimulatedBroker()


def build_orchestrator(settings: Settings, use_mock_decision: bool = False) -> TradingOrchestrator:
    broker = build_broker(settings)
    collector = MarketCollector(broker, settings)
    db = Database(settings.db_path)
    safety = SafetyEngine(settings)
    decisions = WaitDecisionProvider() if use_mock_decision else HTTPDecisionBridge(settings)
    return TradingOrchestrator(broker, collector, decisions, safety, db)
