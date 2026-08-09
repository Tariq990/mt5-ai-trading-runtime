from __future__ import annotations

from .broker.base import Broker
from .broker.mt5_demo import MT5DemoBroker
from .broker.simulated import SimulatedBroker
from .collector import MarketCollector
from .config import Settings
from .db import Database
from .decision.base import DecisionProvider
from .decision.http_bridge import HTTPDecisionBridge
from .decision.mock import WaitDecisionProvider
from .notifications import build_notification_service
from .orchestrator import TradingOrchestrator
from .safety import SafetyEngine


def build_broker(settings: Settings) -> Broker:
    if settings.broker == "mt5":
        return MT5DemoBroker(server_utc_offset_hours=settings.broker_server_utc_offset_hours)
    return SimulatedBroker()


def build_orchestrator(settings: Settings, use_mock_decision: bool = False) -> TradingOrchestrator:
    broker = build_broker(settings)
    collector = MarketCollector(broker, settings)
    db = Database(settings.db_path)
    safety = SafetyEngine(settings)
    if use_mock_decision:
        decisions: DecisionProvider = WaitDecisionProvider()
    else:
        decisions = HTTPDecisionBridge(settings, on_send=db.record_bridge_send)
    notifications = build_notification_service(settings, db)
    return TradingOrchestrator(broker, collector, decisions, safety, db, notifications)
