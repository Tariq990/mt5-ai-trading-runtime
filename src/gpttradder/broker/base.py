from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Mapping, Sequence

from ..models import (
    AccountState,
    Decision,
    ExecutionResult,
    PendingOrder,
    Position,
    Quote,
    SymbolContractSpec,
    SymbolMarketData,
)
from ..symbols import SymbolResolution


class Broker(ABC):
    # Canonical -> broker symbol mapping after the latest resolve_symbols() call.
    _resolution: dict[str, SymbolResolution]

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def assert_demo(self) -> None: ...

    @abstractmethod
    async def get_account_state(self) -> AccountState: ...

    @abstractmethod
    async def get_quote(self, broker_symbol: str) -> Quote: ...

    @abstractmethod
    async def get_market_data(self, broker_symbol: str, timeframes: list[str], limit: int) -> SymbolMarketData: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_pending_orders(self) -> list[PendingOrder]: ...

    @abstractmethod
    async def get_recent_trades(self) -> list[dict]: ...

    @abstractmethod
    async def resolve_symbols(
        self,
        canonical: Sequence[str],
        symbol_map: Mapping[str, str],
    ) -> dict[str, SymbolResolution]: ...

    @abstractmethod
    async def get_contract(self, canonical: str) -> SymbolContractSpec | None: ...

    @abstractmethod
    async def estimate_decision_risk(self, decision: Decision) -> float | None: ...

    @abstractmethod
    async def estimate_decision_margin(self, decision: Decision) -> float | None: ...

    @abstractmethod
    async def estimate_open_risk(self) -> float | None: ...

    @abstractmethod
    async def execute(self, decision: Decision) -> ExecutionResult: ...

    def to_broker_symbol(self, canonical: str) -> str:
        """Map a canonical application symbol to the resolved broker symbol."""
        raise NotImplementedError

    def broker_symbol_to_canonical(self) -> dict[str, str]:
        """Reverse map from resolved broker symbols back to canonical symbols."""
        return {
            res.broker_symbol: res.canonical
            for res in self._resolution.values()
            if res.broker_symbol
        }