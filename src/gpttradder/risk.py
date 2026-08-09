from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from .models import AccountState, Decision, ExposureContext, ExposureGroup, Position, SymbolContractSpec
from .symbols import EXPOSURE_GROUPS


def normalize_volume(requested: float, volume_min: float, volume_max: float, volume_step: float) -> float:
    """Broker-mechanics-only lot normalization. Returns 0.0 when below minimum.

    This may only adjust broker mechanics (step grid, precision). If it would
    materially change ChatGPT's economic intent, the safety engine REJECTS with
    NORMALIZATION_DRIFT instead of silently trading a different size.
    """
    if requested <= 0 or requested < volume_min - 1e-12:
        return 0.0
    step = max(float(volume_step), 1e-12)
    maximum = max(float(volume_min), float(volume_max))
    capped = min(requested, maximum)
    normalized = math.floor((capped - volume_min) / step + 1e-9) * step + volume_min
    step_str = f"{step:.10f}".rstrip("0")
    digits = len(step_str.split(".")[-1]) if "." in step_str else 0
    return round(max(volume_min, min(maximum, normalized)), digits)


def exposure_notional(position: Position, contract: SymbolContractSpec | None) -> float:
    """Notional exposure in the position's price currency.

    Uses the broker contract size when available (1 lot BTC = contract_size BTC,
    1 lot EURUSD = 100_000 EUR) so exposure is comparable across asset classes.
    """
    multiplier = float(contract.trade_contract_size) if contract and contract.trade_contract_size else 1.0
    return float(position.size) * multiplier * float(position.entry_price)


def compute_exposure(
    positions: Sequence[Position],
    contracts: Mapping[str, SymbolContractSpec | None] | None = None,
    canonical_of: Callable[[str], str] | None = None,
) -> ExposureContext:
    """Aggregate deterministic portfolio exposure per asset group.

    Group membership uses canonical symbols only. `canonical_of` translates broker
    symbol names (BTCUSDm -> BTC) when positions still carry broker names.
    `usd_direction` is the USD side implied by positions: all five monitored
    instruments are quoted against USD, so LONG consumes USD, SHORT provides USD.
    """
    contracts = contracts or {}
    groups: dict[str, ExposureGroup] = {
        name: ExposureGroup(group=name, instruments=sorted(members))
        for name, members in EXPOSURE_GROUPS.items()
    }
    total_gross = 0.0
    total_net = 0.0
    usd_direction = 0.0
    for position in positions:
        symbol = position.symbol
        if canonical_of is not None:
            symbol = canonical_of(symbol) or symbol
        notional = exposure_notional(position, contracts.get(symbol))
        signed = notional if position.side.value == "LONG" else -notional
        total_gross += notional
        total_net += signed
        group = None
        for name, members in EXPOSURE_GROUPS.items():
            if symbol in members:
                group = groups[name]
                break
        if group is not None:
            group.gross_exposure += notional
            group.net_exposure += signed
            group.position_count += 1
            usd_direction += -notional if position.side.value == "LONG" else notional
    return ExposureContext(
        groups=groups,
        total_gross_exposure=total_gross,
        total_net_exposure=total_net,
        usd_direction=usd_direction,
    )


@dataclass(frozen=True)
class TradeRiskBreakdown:
    """Broker-native monetary assessment of a proposed ChatGPT entry."""

    symbol: str
    risk_amount: float | None
    risk_percent_of_equity: float | None
    estimated_margin: float | None
    requested_volume: float
    normalized_volume: float | None
    normalization_drift_pct: float | None


async def assess_trade_risk(
    broker,  # Broker protocol: estimate_decision_risk / estimate_decision_margin
    decision: Decision,
    account: AccountState,
    contract: SymbolContractSpec | None,
) -> TradeRiskBreakdown:
    request_size = float(decision.order.size) if decision.order else 0.0
    risk_amount = await broker.estimate_decision_risk(decision)
    margin = await broker.estimate_decision_margin(decision)
    normalized: float | None = None
    if contract is not None and contract.volume_min is not None and contract.volume_step is not None:
        volume_max = contract.volume_max if contract.volume_max is not None else contract.volume_min * 1_000_000.0
        normalized = normalize_volume(request_size, contract.volume_min, volume_max, contract.volume_step)
    drift_pct: float | None = None
    if normalized is not None and request_size > 0:
        drift_pct = abs(normalized - request_size) / request_size * 100.0
    risk_pct: float | None = None
    if risk_amount is not None and account.equity > 0 and decision.risk_percent is not None:
        risk_pct = risk_amount / account.equity * 100.0
    return TradeRiskBreakdown(
        symbol=decision.symbol or "",
        risk_amount=risk_amount,
        risk_percent_of_equity=risk_pct,
        estimated_margin=margin,
        requested_volume=request_size,
        normalized_volume=normalized,
        normalization_drift_pct=drift_pct,
    )