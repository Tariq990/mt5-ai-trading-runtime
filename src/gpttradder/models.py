from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class DecisionAction(StrEnum):
    WAIT = "WAIT"
    LONG = "LONG"
    SHORT = "SHORT"
    MANAGE_POSITION = "MANAGE_POSITION"
    CANCEL_ORDER = "CANCEL_ORDER"
    CLOSE_POSITION = "CLOSE_POSITION"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class ManagementMode(StrEnum):
    STATIC = "STATIC"
    EVERY_5_MIN = "EVERY_5_MIN"
    EVENT_DRIVEN = "EVENT_DRIVEN"
    HIGH_SENSITIVITY = "HIGH_SENSITIVITY"
    NEWS_MODE = "NEWS_MODE"


class ReusePolicy(StrEnum):
    DO_NOT_REUSE = "DO_NOT_REUSE"
    ALLOW_WHILE_VALID = "ALLOW_WHILE_VALID"


class Candle(BaseModel):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


class Quote(BaseModel):
    bid: float
    ask: float
    spread: float
    ts: datetime


class Position(BaseModel):
    position_id: str
    symbol: str
    side: Side
    size: float
    entry_price: float
    current_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    unrealized_pnl: float = 0.0
    opened_at: datetime


class PendingOrder(BaseModel):
    order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    size: float
    price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    created_at: datetime


class AccountState(BaseModel):
    account_id: str = "demo"
    is_demo: bool = True
    balance: float
    equity: float
    free_margin: float
    daily_pnl: float
    peak_equity: float
    trailing_drawdown_pct: float


class SymbolMarketData(BaseModel):
    symbol: str
    quote: Quote
    candles: dict[str, list[Candle]]
    market: dict[str, Any] = Field(default_factory=dict)


class MarketPacket(BaseModel):
    cycle_id: UUID = Field(default_factory=uuid4)
    packet_created_at: datetime = Field(default_factory=utc_now)
    broker_timestamp: datetime
    packet_hash: str | None = None
    symbols: dict[str, SymbolMarketData]
    account: AccountState
    positions: list[Position] = Field(default_factory=list)
    pending_orders: list[PendingOrder] = Field(default_factory=list)
    recent_trades: list[dict[str, Any]] = Field(default_factory=list)
    trigger: Literal["POLL", "EVENT"] = "POLL"
    trigger_reason: str | None = None


class TakeProfitTarget(BaseModel):
    price: float
    close_percent: float = Field(gt=0, le=100)


class OrderInstruction(BaseModel):
    type: OrderType
    entry: float | None = None
    acceptable_price_range: tuple[float, float] | None = None
    size: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self):
        if self.acceptable_price_range:
            lo, hi = self.acceptable_price_range
            if lo > hi:
                raise ValueError("acceptable_price_range lower bound exceeds upper bound")
        return self


class Decision(BaseModel):
    decision_id: UUID = Field(default_factory=uuid4)
    cycle_id: UUID
    decision: DecisionAction
    symbol: str | None = None
    order: OrderInstruction | None = None
    stop_loss: float | None = None
    take_profit: list[TakeProfitTarget] = Field(default_factory=list)
    risk_percent: float | None = Field(default=None, ge=0, le=3)
    management_mode: ManagementMode = ManagementMode.EVENT_DRIVEN
    valid_until: datetime
    reuse_policy: ReusePolicy = ReusePolicy.DO_NOT_REUSE
    confidence: float | None = Field(default=None, ge=0, le=1)
    reason: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_trade_fields(self):
        if self.decision in {DecisionAction.LONG, DecisionAction.SHORT}:
            if not self.symbol or not self.order or self.stop_loss is None:
                raise ValueError("Trade decisions require symbol, order and stop_loss")
        if self.decision == DecisionAction.WAIT and self.order is not None:
            raise ValueError("WAIT cannot contain an order")
        return self


class ExecutionResult(BaseModel):
    decision_id: UUID
    cycle_id: UUID
    status: Literal["FILLED", "REJECTED", "SKIPPED", "PENDING", "CLOSED"]
    broker_ticket: str | None = None
    requested_price: float | None = None
    filled_price: float | None = None
    actual_size: float | None = None
    slippage: float | None = None
    spread: float | None = None
    reason: str | None = None
    ts: datetime = Field(default_factory=utc_now)
