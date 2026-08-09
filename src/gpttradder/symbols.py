from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, Mapping, Sequence

# Canonical application-level instruments. Broker symbols can differ
# (BTCUSDm, XAUUSD.a, XAUUSD- ...) and are resolved at runtime via
# mt5.symbols_get() — never hard-coded per broker.
CANONICAL_SYMBOLS: tuple[str, ...] = ("BTC", "ETH", "XAU", "EURUSD", "GBPUSD")

# Deterministic exposure groups used for aggregate risk context.
EXPOSURE_GROUPS: dict[str, frozenset[str]] = {
    "crypto": frozenset({"BTC", "ETH"}),
    "usd_fx": frozenset({"EURUSD", "GBPUSD"}),
    "metal": frozenset({"XAU"}),
}


def exposure_group_of(symbol: str) -> str | None:
    for group, members in EXPOSURE_GROUPS.items():
        if symbol in members:
            return group
    return None


@dataclass(frozen=True)
class SymbolResolution:
    canonical: str
    broker_symbol: str | None = None
    status: Literal["OK", "NOT_FOUND", "AMBIGUOUS"] = "NOT_FOUND"
    candidates: tuple[str, ...] = ()
    reason: str = ""


def resolve_canonical_symbols(
    available: Iterable[str],
    canonical: Sequence[str] = CANONICAL_SYMBOLS,
    symbol_map: Mapping[str, str] | None = None,
) -> dict[str, SymbolResolution]:
    """Map canonical application symbols to actual broker symbols.

    Resolution order:
    1. explicit `symbol_map` override (the mapped name must exist at the broker);
    2. exact broker name match;
    3. prefix match where the remainder contains only letters/digits/./-/_ so that
       BTCUSD, BTCUSDm, XAUUSD.a etc. all resolve from BTC / XAU.

    Ambiguity (multiple plausible broker symbols) fails closed: a clear exact
    match is the only silent winner, everything else must be disambiguated with
    an explicit symbol_map entry.
    """
    available_set = {str(name).strip().upper() for name in available if str(name).strip()}
    explicit = {str(k).strip().upper(): str(v).strip().upper() for k, v in dict(symbol_map or {}).items()}

    results: dict[str, SymbolResolution] = {}
    for symbol in canonical:
        symbol = str(symbol).strip().upper()
        if not symbol:
            continue
        if symbol in explicit:
            mapped = explicit[symbol]
            if mapped in available_set:
                results[symbol] = SymbolResolution(
                    canonical=symbol,
                    broker_symbol=mapped,
                    status="OK",
                    reason="explicit symbol_map override",
                )
            else:
                results[symbol] = SymbolResolution(
                    canonical=symbol,
                    status="NOT_FOUND",
                    reason=f"symbol_map override {mapped} is not available at the broker",
                )
            continue
        if symbol in available_set:
            results[symbol] = SymbolResolution(
                canonical=symbol, broker_symbol=symbol, status="OK", reason="exact broker name match"
            )
            continue
        candidates = tuple(
            sorted(
                name
                for name in available_set
                if name.startswith(symbol)
                and len(name) > len(symbol)
                and all(ch.isalnum() or ch in "._-" for ch in name[len(symbol):])
            )
        )
        if len(candidates) == 1:
            results[symbol] = SymbolResolution(
                canonical=symbol,
                broker_symbol=candidates[0],
                status="OK",
                reason=f"suffix match -> {candidates[0]}",
            )
        elif len(candidates) > 1:
            results[symbol] = SymbolResolution(
                canonical=symbol,
                status="AMBIGUOUS",
                candidates=candidates,
                reason="multiple plausible broker symbols; configure GPTTRADDER_SYMBOL_MAP to disambiguate",
            )
        else:
            results[symbol] = SymbolResolution(
                canonical=symbol, status="NOT_FOUND", reason="no matching broker symbol (use GPTTRADDER_SYMBOL_MAP to override)"
            )
    return results