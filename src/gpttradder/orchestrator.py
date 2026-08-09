from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from .broker.base import Broker
from .collector import MarketCollector
from .db import Database
from .decision.base import DecisionProvider
from .hashing import market_packet_hash
from .models import Decision, DecisionAction, ExecutionResult, MarketPacket
from .notifications import NotificationService
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
        notifications: NotificationService | None = None,
    ):
        self.broker = broker
        self.collector = collector
        self.decisions = decisions
        self.safety = safety
        self.db = db
        self.notifications = notifications
        self._cycle_lock = asyncio.Lock()

    async def run_cycle(self, trigger: str = "POLL", reason: str | None = None) -> ExecutionResult | None:
        if self._cycle_lock.locked():
            logger.warning("Cycle skipped because another cycle is already active")
            return None

        owner = uuid4().hex
        lock_name = "global-trading-cycle"
        if not self.db.try_acquire_lock(lock_name, owner, self.safety.settings.cycle_lock_ttl_seconds):
            logger.warning("Cycle skipped because the cross-process cycle lock is active")
            return None

        try:
            async with self._cycle_lock:
                return await self._run_locked(trigger=trigger, reason=reason)
        finally:
            self.db.release_lock(lock_name, owner)

    async def _run_locked(self, trigger: str, reason: str | None) -> ExecutionResult | None:
        try:
            packet = await self.collector.collect(trigger=trigger, reason=reason)
        except Exception as exc:
            logger.exception("Market collection failed")
            await self._notify_runtime(
                f"Market collection failed: {type(exc).__name__}: {exc}",
                severity="ERROR",
                key="market-collection-failed",
            )
            return None

        packet.account = self.db.apply_risk_state(packet.account, self.safety.settings.risk_timezone)
        packet.decision_history = self.db.get_recent_decisions(limit=20)
        packet.packet_hash = market_packet_hash(packet)
        self.db.save_cycle(packet, packet.packet_hash)

        packet_gate = self.safety.packet_gate(packet)
        if not packet_gate.allowed:
            self.db.update_cycle_status(packet.cycle_id, packet_gate.code)
            logger.warning("Cycle blocked: %s", packet_gate.reason)
            await self._notify_runtime(
                f"Cycle blocked [{packet_gate.code}]: {packet_gate.reason}",
                severity="WARNING",
                key=f"cycle-block:{packet_gate.code}",
            )
            return None

        try:
            decision = await self.decisions.decide(packet)
        except Exception as exc:
            self.db.update_cycle_status(packet.cycle_id, "DECISION_FAILED")
            logger.exception("Decision provider failed")
            await self._notify_runtime(
                f"ChatGPT decision provider failed: {type(exc).__name__}: {exc}",
                severity="ERROR",
                key="decision-provider-failed",
            )
            return None

        if not self.db.save_decision(decision):
            self.db.update_cycle_status(packet.cycle_id, "DUPLICATE_DECISION")
            await self._notify_runtime(
                f"Duplicate decision rejected: {decision.decision_id}",
                severity="WARNING",
                key=f"duplicate-decision:{decision.decision_id}",
            )
            return None

        # Reserve the decision before any broker side effect. This is the hard
        # at-most-once boundary even across multiple Python processes.
        if not self.db.claim_execution(decision):
            self.db.update_cycle_status(packet.cycle_id, "ALREADY_CLAIMED")
            await self._notify_runtime(
                f"Execution already claimed: {decision.decision_id}",
                severity="WARNING",
                key=f"claimed-decision:{decision.decision_id}",
            )
            return None

        gate = self.safety.decision_gate(packet, decision)
        if not gate.allowed:
            return await self._finalize_rejection(packet, decision, gate.code, gate.reason)

        if decision.decision == DecisionAction.WAIT:
            result = ExecutionResult(
                decision_id=decision.decision_id,
                cycle_id=decision.cycle_id,
                status="SKIPPED",
                reason="WAIT",
            )
            self.db.finalize_execution(result)
            self.db.update_cycle_status(packet.cycle_id, result.status)
            await self._notify_result(packet, decision, result)
            return result

        try:
            await self.broker.assert_demo()

            # A fresh broker quote is mandatory immediately before a new entry.
            if decision.decision in {DecisionAction.LONG, DecisionAction.SHORT}:
                assert decision.symbol is not None
                fresh_quote = await self.broker.get_quote(self.broker.to_broker_symbol(decision.symbol))
                price_gate = self.safety.execution_price_gate(decision, fresh_quote)
                if not price_gate.allowed:
                    return await self._finalize_rejection(packet, decision, price_gate.code, price_gate.reason)

                fresh_account = self.db.apply_risk_state(
                    await self.broker.get_account_state(),
                    self.safety.settings.risk_timezone,
                )
                account_gate = self.safety.account_gate(fresh_account)
                if not account_gate.allowed:
                    return await self._finalize_rejection(packet, decision, account_gate.code, account_gate.reason)

                decision_risk, open_risk = await asyncio.gather(
                    self.broker.estimate_decision_risk(decision),
                    self.broker.estimate_open_risk(),
                )
                risk_gate = self.safety.risk_budget_gate(
                    fresh_account,
                    decision,
                    decision_risk,
                    open_risk,
                )
                if not risk_gate.allowed:
                    return await self._finalize_rejection(packet, decision, risk_gate.code, risk_gate.reason)

                # Broker-native monetary breakdown (SL risk, margin, normalized
                # volume, drift) is persisted onto the decision so ChatGPT sees it
                # in the next cycle's decision history.
                try:
                    from .risk import assess_trade_risk

                    breakdown = await assess_trade_risk(
                        self.broker,
                        decision,
                        fresh_account,
                        packet.symbols[decision.symbol].contract,
                    )
                    logger.info(
                        "Risk breakdown %s: risk=%.2f margin=%s volume=%s->%s drift=%s%%",
                        breakdown.symbol,
                        breakdown.risk_amount or 0.0,
                        breakdown.estimated_margin,
                        breakdown.requested_volume,
                        breakdown.normalized_volume,
                        round(breakdown.normalization_drift_pct, 3) if breakdown.normalization_drift_pct is not None else "n/a",
                    )
                    self.db.update_decision_metadata(decision, {"risk_breakdown": breakdown.__dict__})
                except Exception:
                    logger.exception("Broker risk breakdown failed")

            result = await self.broker.execute(decision)
        except Exception as exc:
            logger.exception("Broker execution failed")
            result = ExecutionResult(
                decision_id=decision.decision_id,
                cycle_id=decision.cycle_id,
                status="REJECTED",
                reason=f"BROKER_EXCEPTION: {type(exc).__name__}: {exc}",
            )

        self.db.finalize_execution(result)
        self.db.update_cycle_status(packet.cycle_id, result.status)
        await self._notify_result(packet, decision, result)
        return result

    async def _finalize_rejection(
        self,
        packet: MarketPacket,
        decision: Decision,
        code: str,
        reason: str,
    ) -> ExecutionResult:
        result = ExecutionResult(
            decision_id=decision.decision_id,
            cycle_id=decision.cycle_id,
            status="REJECTED",
            reason=f"{code}: {reason}",
        )
        self.db.finalize_execution(result)
        self.db.update_cycle_status(decision.cycle_id, code)
        await self._notify_result(packet, decision, result)
        return result

    async def _notify_result(self, packet: MarketPacket, decision: Decision, result: ExecutionResult) -> None:
        if self.notifications is None:
            return
        try:
            await self.notifications.decision_result(packet, decision, result)
        except Exception:
            logger.exception("Decision notification failed")

    async def _notify_runtime(
        self,
        message: str,
        *,
        severity: str,
        key: str | None = None,
    ) -> None:
        if self.notifications is None:
            return
        try:
            await self.notifications.runtime(message, severity=severity, key=key)
        except Exception:
            logger.exception("Runtime notification failed")
