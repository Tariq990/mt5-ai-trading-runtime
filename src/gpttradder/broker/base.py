from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import AccountState, Decision, ExecutionResult, PendingOrder, Position, SymbolMarketData


class Broker(ABC):
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def assert_demo(self) -> None: ...

    @abstractmethod
    async def get_account_state(self) -> AccountState: ...

    @abstractmethod
    async def get_market_data(self, symbol: str, timeframes: list[str], limit: int) -> SymbolMarketData: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_pending_orders(self) -> list[PendingOrder]: ...

    @abstractmethod
    async def get_recent_trades(self) -> list[dict]: ...

    @abstractmethod
    async def execute(self, decision: Decision) -> ExecutionResult: ...
