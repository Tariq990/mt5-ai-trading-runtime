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


class ManagementAction(StrEnum):
    HOLD = "HOLD"
    MOVE_SL = "MOVE_SL"
    MOVE_TP = "MOVE_TP"
    BREAK_EVEN = "BREAK_EVEN"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    FULL_CLOSE = "FULL_CLOSE"


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

    @model_validator(mode="after")
    def validate_quote(self):
        if self.ask < self.bid:
            raise ValueError("ask must be greater than or equal to bid")
        return self


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
    account_mode: str | None = None
    hedging_allowed: bool | None = None
    daily_pnl: float = 0.0
    daily_start_equity: float | None = None
    peak_equity: float
    trailing_drawdown_pct: float


class SymbolContractSpec(BaseModel):
    """Broker contract metadata for one instrument (from mt5.symbol_info)."""

    broker_symbol: str
    canonical_symbol: str
    is_tradable: bool = True
    description: str | None = None
    path: str | None = None
    # Price / precision
    point: float | None = None
    digits: int | None = None
    trade_tick_size: float | None = None
    # Tick value
    trade_tick_value: float | None = None
    trade_tick_value_profit: float | None = None
    trade_tick_value_loss: float | None = None
    # Contract
    trade_contract_size: float | None = None
    # Volume
    volume_min: float | None = None
    volume_max: float | None = None
    volume_step: float | None = None
    volume_limit: float | None = None
    # Broker constraints
    trade_stops_level: int | None = None
    trade_freeze_level: int | None = None
    trade_mode: int | None = None
    trade_mode_label: str | None = None
    trade_calc_mode: int | None = None
    filling_mode: int | None = None
    order_mode: int | None = None
    expiration_mode: int | None = None
    trade_exemode: int | None = None
    # Currency
    currency_base: str | None = None
    currency_profit: str | None = None
    currency_margin: str | None = None
    # Swap / carry
    swap_mode: int | None = None
    swap_long: float | None = None
    swap_short: float | None = None
    swap_rollover3days: int | None = None
    # Margin
    margin_initial: float | None = None
    margin_maintenance: float | None = None


class SymbolMarketData(BaseModel):
    symbol: str
    quote: Quote
    candles: dict[str, list[Candle]]
    market: dict[str, Any] = Field(default_factory=dict)
    contract: SymbolContractSpec | None = None


class SymbolStatus(BaseModel):
    """Per-symbol availability, decomposed so each gate is explicit and the
    effective verdict fails closed.

    - status: coarse broker-side verdict (TRADABLE / NOT_TRADABLE / NO_QUOTE /
      TIME_INVALID / UNRESOLVED / AMBIGUOUS).
    - broker_trade_mode: raw broker trade mode (FULL/LONGONLY/SHORTONLY/
      CLOSEONLY/DISABLED) — holiday closures show up here as CLOSEONLY/DISABLED.
    - quote_fresh: quote timestamp within [just-now, max_age] after broker-time
      normalization; future-stamped quotes are never fresh.
    - market_session_open: calendar check (crypto 24/7; FX-style weekend
      closure for currency/metals / unclassified symbols).
    - executable_now: aggregated entry verdict — True only when the symbol may
      accept NEW LONG/SHORT risk right now. Protective management, closes and
      order cancels intentionally stay available regardless.
    """

    canonical: str
    broker_symbol: str | None = None
    status: Literal["TRADABLE", "NO_QUOTE", "TIME_INVALID", "UNRESOLVED", "AMBIGUOUS", "NOT_TRADABLE"]
    reason: str | None = None
    broker_trade_mode: str | None = None
    quote_fresh: bool = True
    quote_age_seconds: float | None = None
    market_session_open: bool = True
    executable_now: bool = False


class ExposureGroup(BaseModel):
    group: str
    instruments: list[str] = Field(default_factory=list)
    gross_exposure: float = 0.0
    net_exposure: float = 0.0
    position_count: int = 0


class ExposureContext(BaseModel):
    groups: dict[str, ExposureGroup] = Field(default_factory=dict)
    total_gross_exposure: float = 0.0
    total_net_exposure: float = 0.0
    # Positive = net short USD exposure, negative = net long USD exposure
    # (all monitored instruments are USD-quoted pairs).
    usd_direction: float | None = None


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
    decision_history: list[dict[str, Any]] = Field(default_factory=list)
    trigger: Literal["POLL", "EVENT"] = "POLL"
    trigger_reason: str | None = None
    exposure: ExposureContext | None = None
    symbol_status: dict[str, SymbolStatus] = Field(default_factory=dict)


class TakeProfitTarget(BaseModel):
    price: float
    close_percent: float = Field(gt=0, le=100)


class OrderInstruction(BaseModel):
    type: OrderType
    entry: float | None = None
    acceptable_price_range: tuple[float, float] | None = None
    size: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_order(self):
        if self.acceptable_price_range:
            lo, hi = self.acceptable_price_range
            if lo > hi:
                raise ValueError("acceptable_price_range lower bound exceeds upper bound")
        if self.type in {OrderType.LIMIT, OrderType.STOP} and self.entry is None:
            raise ValueError("LIMIT and STOP orders require an entry price")
        return self


class ManagementInstruction(BaseModel):
    action: ManagementAction
    position_id: str
    stop_loss: float | None = None
    take_profit: float | None = None
    close_percent: float | None = Field(default=None, gt=0, le=100)

    @model_validator(mode="after")
    def validate_management(self):
        if self.action == ManagementAction.MOVE_SL and self.stop_loss is None:
            raise ValueError("MOVE_SL requires stop_loss")
        if self.action == ManagementAction.MOVE_TP and self.take_profit is None:
            raise ValueError("MOVE_TP requires take_profit")
        if self.action == ManagementAction.PARTIAL_CLOSE:
            if self.close_percent is None or self.close_percent >= 100:
                raise ValueError("PARTIAL_CLOSE requires close_percent below 100")
        return self


class Decision(BaseModel):
    decision_id: UUID = Field(default_factory=uuid4)
    cycle_id: UUID
    decision: DecisionAction
    symbol: str | None = None
    order: OrderInstruction | None = None
    stop_loss: float | None = None
    take_profit: list[TakeProfitTarget] = Field(default_factory=list)
    risk_percent: float | None = Field(default=None, gt=0, le=3)
    management: ManagementInstruction | None = None
    position_id: str | None = None
    pending_order_id: str | None = None
    management_mode: ManagementMode = ManagementMode.EVENT_DRIVEN
    valid_until: datetime
    reuse_policy: ReusePolicy = ReusePolicy.DO_NOT_REUSE
    confidence: float | None = Field(default=None, ge=0, le=1)
    reason: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_decision_fields(self):
        if self.valid_until.tzinfo is None or self.valid_until.utcoffset() is None:
            raise ValueError("valid_until must be timezone-aware")
        if self.decision in {DecisionAction.LONG, DecisionAction.SHORT}:
            if not self.symbol or not self.order or self.stop_loss is None or self.risk_percent is None:
                raise ValueError("Trade decisions require symbol, order, stop_loss and risk_percent")
            if self.management is not None:
                raise ValueError("Entry decisions cannot contain management")
            if self.take_profit:
                total = sum(target.close_percent for target in self.take_profit)
                if total > 100.000001:
                    raise ValueError("take_profit close_percent total cannot exceed 100")
        elif self.decision == DecisionAction.WAIT:
            if any((self.order, self.management, self.position_id, self.pending_order_id)):
                raise ValueError("WAIT cannot contain execution instructions")
        elif self.decision == DecisionAction.MANAGE_POSITION:
            if not self.symbol or self.management is None:
                raise ValueError("MANAGE_POSITION requires symbol and management")
        elif self.decision == DecisionAction.CLOSE_POSITION:
            if not self.symbol or not self.position_id:
                raise ValueError("CLOSE_POSITION requires symbol and position_id")
        elif self.decision == DecisionAction.CANCEL_ORDER:
            if not self.symbol or not self.pending_order_id:
                raise ValueError("CANCEL_ORDER requires symbol and pending_order_id")
        return self


class ExecutionResult(BaseModel):
    decision_id: UUID
    cycle_id: UUID
    status: Literal["FILLED", "REJECTED", "SKIPPED", "PENDING", "CLOSED", "CANCELLED", "MODIFIED"]
    broker_ticket: str | None = None
    requested_price: float | None = None
    filled_price: float | None = None
    actual_size: float | None = None
    slippage: float | None = None
    spread: float | None = None
    reason: str | None = None
    ts: datetime = Field(default_factory=utc_now)
