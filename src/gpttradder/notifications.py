from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from .config import Settings
from .db import Database
from .models import Decision, DecisionAction, ExecutionResult, MarketPacket

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    async def healthcheck(self) -> tuple[bool, str]: ...
    async def send(self, text: str) -> bool: ...


class NullNotifier:
    async def healthcheck(self) -> tuple[bool, str]:
        return True, "disabled"

    async def send(self, text: str) -> bool:
        return False


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not token or not chat_id:
            raise ValueError("Telegram token and chat_id are required")
        self.token = token
        self.chat_id = chat_id
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    @property
    def _base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    async def healthcheck(self) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = await client.get(f"{self._base_url}/getMe")
                response.raise_for_status()
                payload = response.json()
            if not payload.get("ok"):
                return False, str(payload.get("description") or "Telegram getMe failed")
            username = (payload.get("result") or {}).get("username") or "bot"
            return True, f"@{username}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    async def send(self, text: str) -> bool:
        # Telegram sendMessage currently limits text messages to 4096 characters.
        text = str(text).strip()
        if not text:
            return False
        chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                for chunk in chunks:
                    response = await client.post(
                        f"{self._base_url}/sendMessage",
                        json={"chat_id": self.chat_id, "text": chunk},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not payload.get("ok"):
                        raise RuntimeError(payload.get("description") or "Telegram sendMessage failed")
            return True
        except Exception:
            # Notifications must never break the trading safety path.
            logger.exception("Telegram notification failed")
            return False


@dataclass
class NotificationService:
    notifier: Notifier
    db: Database
    cooldown_seconds: int = 300
    notify_wait: bool = False

    async def healthcheck(self) -> tuple[bool, str]:
        return await self.notifier.healthcheck()

    async def send_once(self, key: str, text: str, *, cooldown_seconds: int | None = None) -> bool:
        cooldown = self.cooldown_seconds if cooldown_seconds is None else cooldown_seconds
        if not self.db.acquire_notification_slot(key, cooldown):
            return False
        return await self.notifier.send(text)

    async def runtime(self, message: str, *, severity: str = "INFO", key: str | None = None) -> bool:
        text = f"GPTTRADDER [{severity}]\n{message}"
        if key:
            return await self.send_once(key, text)
        return await self.notifier.send(text)

    async def decision_result(
        self,
        packet: MarketPacket,
        decision: Decision,
        result: ExecutionResult,
    ) -> bool:
        if decision.decision == DecisionAction.WAIT and not self.notify_wait:
            return False
        symbol = decision.symbol or "-"
        lines = [
            "GPTTRADDER Decision",
            f"Action: {decision.decision.value}",
            f"Symbol: {symbol}",
            f"Result: {result.status}",
            f"Confidence: {decision.confidence if decision.confidence is not None else '-'}",
            f"Daily PnL: {packet.account.daily_pnl:.2f}",
            f"Trailing DD: {packet.account.trailing_drawdown_pct:.2f}%",
        ]
        status = packet.symbol_status.get(symbol) if symbol else None
        if status and status.broker_symbol:
            lines.append(f"Broker symbol: {status.broker_symbol}")
        contract = None
        market_data = packet.symbols.get(symbol) if symbol else None
        if market_data is not None:
            contract = market_data.contract
        if contract is not None and contract.volume_min is not None:
            lines.append(
                f"Contract: vol {contract.volume_min}-{contract.volume_max} "
                f"step {contract.volume_step} | size {contract.trade_contract_size} "
                f"tick {contract.trade_tick_value}"
            )
        if packet.exposure is not None and packet.exposure.total_gross_exposure:
            lines.append(
                f"Exposure: gross {packet.exposure.total_gross_exposure:.2f} | "
                f"net {packet.exposure.total_net_exposure:.2f}"
            )
        if result.filled_price is not None:
            lines.append(f"Fill: {result.filled_price}")
        if result.broker_ticket:
            lines.append(f"Ticket: {result.broker_ticket}")
        if result.reason and result.reason != "WAIT":
            lines.append(f"Reason: {result.reason[:500]}")
        return await self.notifier.send("\n".join(lines))


def build_notification_service(settings: Settings, db: Database) -> NotificationService:
    if not settings.telegram_enabled:
        notifier: Notifier = NullNotifier()
    else:
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            # Fail closed at preflight/startup rather than silently dropping requested alerts.
            notifier = NullNotifier()
        else:
            notifier = TelegramNotifier(
                settings.telegram_bot_token.get_secret_value(),
                settings.telegram_chat_id,
                timeout_seconds=settings.telegram_timeout_seconds,
            )
    return NotificationService(
        notifier=notifier,
        db=db,
        cooldown_seconds=settings.notification_cooldown_seconds,
        notify_wait=settings.telegram_notify_wait,
    )
