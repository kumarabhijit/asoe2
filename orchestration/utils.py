from __future__ import annotations

from contracts.models import CircuitBreakerDecision, PricingDiscrepancy
from contracts.policy import (
    CIRCUIT_BREAKER_MAX_UPDATES,
    CIRCUIT_BREAKER_MAX_VARIANCE,
    DISCREPANCY_THRESHOLD,
)


def compute_discrepancy(po_price: float, sap_base_price: float) -> PricingDiscrepancy:
    delta = round(po_price - sap_base_price, 4)
    pct = 0.0 if sap_base_price == 0 else round(abs(delta) / sap_base_price, 6)
    return PricingDiscrepancy(delta=delta, delta_pct=pct, within_threshold=pct <= DISCREPANCY_THRESHOLD)


def circuit_breaker(update_count: int, batch_total_variance: float) -> CircuitBreakerDecision:
    reasons = []
    if update_count > CIRCUIT_BREAKER_MAX_UPDATES:
        reasons.append(f"Max {CIRCUIT_BREAKER_MAX_UPDATES} updates / 5-minute window exceeded.")
    if batch_total_variance > CIRCUIT_BREAKER_MAX_VARIANCE:
        reasons.append(f"Total dollar variance exceeds ${CIRCUIT_BREAKER_MAX_VARIANCE:,.0f}.")
    return CircuitBreakerDecision(allowed=not reasons, reasons=reasons)
