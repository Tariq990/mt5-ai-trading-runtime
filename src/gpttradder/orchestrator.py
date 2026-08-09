from __future__ import annotations

import asyncio
import logging

from .broker.base import Broker
from .collector import MarketCollector
from .db import Database
from .decision.base import DecisionProvider
from .models import DecisionAction, ExecutionResult
from .safety import SafetyEngine

logger = logging.getLogger(__name__)


class TradingOrchestrator:
    def __init__(
        self,
        broker: Broker,
        collector: MarketCollector,
        decisions: DecisionProvider,
        safety: SafetyEngine,
        db: Database,
    ):
        self.broker = broker
        self.collector = collector
        self.decisions = decisions
        self.safety = safety
        self.db = db
        self._cycle_lock = asyncio.Lock()

    async def run_cycle(self, trigger: str = "POLL", reason: str | None = None) -> ExecutionResult | None:
        if self._cycle_lock.locked():
            logger.warning("Cycle skipped because another cycle is already active")
            return None
        async with self._cycle_lock:
            packet = await self.collector.collect(trigger=trigger, reason=reason)
            assert packet.packet_hash
            self.db.save_cycle(packet, packet.packet_hash)

            packet_gate = self.safety.packet_gate(packet)
            if not packet_gate.allowed:
                self.db.update_cycle_status(packet.cycle_id, packet_gate.code)
                logger.warning("Cycle blocked: %s", packet_gate.reason)
                return None

            try:
                decision = await self.decisions.decide(packet)
            except Exception:
                self.db.update_cycle_status(packet.cycle_id, "DECISION_FAILED")
                logger.exception("Decision provider failed")
                return None

            if not self.db.save_decision(decision):
                self.db.update_cycle_status(packet.cycle_id, "DUPLICATE_DECISION")
                return None

            if self.db.was_executed(decision.decision_id):
                self.db.update_cycle_status(packet.cycle_id, "ALREADY_EXECUTED")
                return None

            gate = self.safety.decision_gate(packet, decision)
            if not gate.allowed:
                result = ExecutionResult(
                    decision_id=decision.decision_id,
                    cycle_id=decision.cycle_id,
                    status="REJECTED",
                    reason=f"{gate.code}: {gate.reason}",
                )
                self.db.save_execution(result)
                self.db.update_cycle_status(packet.cycle_id, gate.code)
                return result

            if decision.decision == DecisionAction.WAIT:
                result = ExecutionResult(
                    decision_id=decision.decision_id,
                    cycle_id=decision.cycle_id,
                    status="SKIPPED",
                    reason="WAIT",
                )
            else:
                # Refresh demo-account guard immediately before any broker write.
                await self.broker.assert_demo()
                result = await self.broker.execute(decision)

            self.db.save_execution(result)
            self.db.update_cycle_status(packet.cycle_id, result.status)
            return result
