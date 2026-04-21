from __future__ import annotations

# Back-Order (OOS) Resolution Recipe
#
# Deterministic classification + resolution-option ranking for a line
# that cannot be fully fulfilled from the primary DC.
#
# Inputs:
#   order_id              : str          — host ERP order identifier
#   sku                   : str          — material number
#   ordered_qty           : float        — quantity requested on PO
#   available_qty         : float        — on-hand at the primary DC
#   unit_price            : float        — used for at-risk computation
#   uom                   : str          — unit of measure (echoed)
#   severe_gap_pct        : float        — policy threshold (SD-OOS-002)
#   alternate_warehouses  : List[Dict]   — {plant, qty, eta_days,
#                                          freight_delta_per_unit}
#   substitutes           : List[Dict]   — {sku, available_qty,
#                                          price_delta_pct,
#                                          acceptance_rate}
#
# Outputs (Dict[str, Any]):
#   status                : "REVIEW_REQUIRED" | "ESCALATE" | "FAILED"
#   classification        : "MINOR_GAP" | "SEVERE_GAP"
#   gap_qty               : float        — ordered − available
#   gap_pct               : float        — gap_qty / ordered_qty
#   at_risk               : float        — gap_qty × unit_price
#   primary_option        : Dict         — the top-ranked resolution
#   resolution_options    : List[Dict]   — ranked candidates, best first
#   recommended_action    : "SPLIT_SHIPMENT" | "ALT_DC" | "SUBSTITUTE" |
#                           "RESCHEDULE"
#
# Invariants:
#   - ordered_qty must be > 0 (validated upstream in validate_types).
#   - No LLM calls, no I/O.
#   - Ranks alternate DCs by total freight delta (lower is better).

from typing import Any, Dict, List


def resolve_back_order(
    order_id: str,
    sku: str,
    ordered_qty: float,
    available_qty: float,
    unit_price: float,
    uom: str,
    severe_gap_pct: float,
    alternate_warehouses: List[Dict[str, Any]] | None = None,
    substitutes: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Classify a back-order and rank resolution options.

    Three-tier decision:
      gap_pct <  severe_gap_pct        → REVIEW_REQUIRED (SD-OOS-001)
      gap_pct >= severe_gap_pct        → ESCALATE        (SD-OOS-002)
      ordered_qty <= 0 or invalid      → FAILED (FAIL_TO_HUMAN upstream)
    """
    if ordered_qty <= 0 or available_qty < 0:
        return {
            "status": "FAILED",
            "classification": None,
            "gap_qty": None,
            "gap_pct": None,
            "at_risk": None,
            "primary_option": None,
            "resolution_options": [],
            "recommended_action": None,
            "order_id": order_id,
            "reason": (
                f"ordered_qty must be > 0 and available_qty >= 0; "
                f"got {ordered_qty}, {available_qty}."
            ),
        }

    gap_qty = max(0.0, ordered_qty - available_qty)
    gap_pct = gap_qty / ordered_qty if ordered_qty > 0 else 0.0
    at_risk = round(gap_qty * unit_price, 2)

    if gap_qty == 0:
        return {
            "status": "COMPLETE",
            "classification": "NO_GAP",
            "gap_qty": 0.0,
            "gap_pct": 0.0,
            "at_risk": 0.0,
            "primary_option": None,
            "resolution_options": [],
            "recommended_action": None,
            "order_id": order_id,
            "sku": sku,
            "uom": uom,
        }

    classification = (
        "SEVERE_GAP" if gap_pct >= severe_gap_pct else "MINOR_GAP"
    )

    options = _build_options(
        gap_qty=gap_qty,
        unit_price=unit_price,
        alternate_warehouses=alternate_warehouses or [],
        substitutes=substitutes or [],
    )
    primary = options[0] if options else None
    recommended_action = primary["type"] if primary else "RESCHEDULE"

    status = "ESCALATE" if classification == "SEVERE_GAP" else "REVIEW_REQUIRED"

    return {
        "status": status,
        "classification": classification,
        "gap_qty": round(gap_qty, 3),
        "gap_pct": round(gap_pct, 4),
        "at_risk": at_risk,
        "primary_option": primary,
        "resolution_options": options,
        "recommended_action": recommended_action,
        "order_id": order_id,
        "sku": sku,
        "uom": uom,
    }


def _build_options(
    gap_qty: float,
    unit_price: float,
    alternate_warehouses: List[Dict[str, Any]],
    substitutes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Assemble ranked resolution options for the gap quantity."""
    options: List[Dict[str, Any]] = []

    # Alt-DC: one option per alternate warehouse with at least gap_qty.
    for i, dc in enumerate(alternate_warehouses):
        qty = float(dc.get("qty") or 0.0)
        if qty < gap_qty:
            continue
        freight_delta = float(dc.get("freight_delta_per_unit") or 0.0)
        total_freight = round(freight_delta * gap_qty, 2)
        options.append({
            "id": f"alt-dc-{i + 1}",
            "type": "ALT_DC",
            "title": f"Ship gap from {dc.get('plant', 'alternate DC')}",
            "source": dc.get("plant"),
            "eta_days": dc.get("eta_days"),
            "extra_cost": total_freight,
            "composite_score": _score(
                service=0.85,
                revenue_preservation=1.0,
                logistics_penalty=total_freight / (unit_price * gap_qty + 1e-9),
            ),
        })

    # Substitute: one option per acceptable substitute.
    for i, sub in enumerate(substitutes):
        sub_qty = float(sub.get("available_qty") or 0.0)
        if sub_qty < gap_qty:
            continue
        price_delta_pct = float(sub.get("price_delta_pct") or 0.0)
        acceptance_rate = float(sub.get("acceptance_rate") or 0.0)
        options.append({
            "id": f"substitute-{i + 1}",
            "type": "SUBSTITUTE",
            "title": f"Substitute with {sub.get('sku', 'alternate SKU')}",
            "substitute_sku": sub.get("sku"),
            "price_delta_pct": price_delta_pct,
            "acceptance_rate": acceptance_rate,
            "extra_cost": round(price_delta_pct * unit_price * gap_qty, 2),
            "composite_score": _score(
                service=acceptance_rate,
                revenue_preservation=1.0 - abs(price_delta_pct),
                logistics_penalty=abs(price_delta_pct) * 0.5,
            ),
        })

    # Partial / split-shipment fallback — always available.
    options.append({
        "id": "split-shipment",
        "type": "SPLIT_SHIPMENT",
        "title": "Ship available qty now, backorder the gap",
        "extra_cost": 0.0,
        "composite_score": _score(
            service=0.60,
            revenue_preservation=0.70,
            logistics_penalty=0.1,
        ),
    })

    # Reschedule to next ATP window — always available.
    options.append({
        "id": "reschedule",
        "type": "RESCHEDULE",
        "title": "Reschedule entire order to next ATP window",
        "extra_cost": 0.0,
        "composite_score": _score(
            service=0.40,
            revenue_preservation=1.0,
            logistics_penalty=0.0,
        ),
    })

    # Rank descending by composite_score.
    options.sort(key=lambda o: o["composite_score"], reverse=True)
    return options


def _score(
    service: float,
    revenue_preservation: float,
    logistics_penalty: float,
) -> float:
    """Weighted aggregate for option ranking. Higher = better."""
    return round(
        0.50 * service + 0.35 * revenue_preservation - 0.15 * logistics_penalty,
        4,
    )
