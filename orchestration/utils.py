from __future__ import annotations

from contracts.models import CircuitBreakerDecision, PricingDiscrepancy


def compute_discrepancy(po_price: float, sap_base_price: float) -> PricingDiscrepancy:
    delta = round(po_price - sap_base_price, 4)
    pct = 0.0 if sap_base_price == 0 else round(abs(delta) / sap_base_price, 6)
    return PricingDiscrepancy(delta=delta, delta_pct=pct, within_threshold=pct <= 0.15)


def circuit_breaker(update_count: int, batch_total_variance: float) -> CircuitBreakerDecision:
    reasons = []
    if update_count > 50:
        reasons.append("Max 50 updates / 5-minute window exceeded.")
    if batch_total_variance > 10_000:
        reasons.append("Total dollar variance exceeds $10,000.")
    return CircuitBreakerDecision(allowed=not reasons, reasons=reasons)
