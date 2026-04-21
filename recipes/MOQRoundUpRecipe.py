from __future__ import annotations

# Minimum Order Quantity (MOQ) Round-Up Recipe
#
# Deterministic decision: when a PO line falls below the KNMT-MINBM
# minimum order quantity, should we (a) round-up to the MOQ, (b) accept
# the order below MOQ per customer concession, or (c) escalate for a
# waiver / sales-manager approval?
#
# Inputs:
#   order_id              : str    — host ERP order id
#   sku                   : str
#   ordered_qty           : float
#   moq_qty               : float  — KNMT-MINBM minimum
#   unit_cost             : float  — used for at_risk + uplift valuation
#   uom                   : str    — echoed
#   severe_shortfall_pct  : float  — SD-MOQ-002 threshold (default 0.25)
#   uplift_review_pct     : float  — round-up uplift above which
#                                    operator sign-off is required
#                                    even if shortfall < severe
#
# Outputs (Dict[str, Any]):
#   status             : "COMPLETE" | "REVIEW_REQUIRED" | "ESCALATE" | "FAILED"
#   classification     : "NO_SHORTFALL" | "MINOR_SHORTFALL" | "SEVERE_SHORTFALL"
#   shortfall_qty      : float
#   shortfall_pct      : float   — shortfall / moq_qty
#   uplift_qty         : float   — additional cases to hit MOQ
#   uplift_pct         : float   — uplift / ordered_qty
#   uplift_value       : float   — uplift_qty × unit_cost
#   recommended_action : "ROUND_UP" | "ACCEPT_BELOW" | "ESCALATE"
#   round_up_plan      : Dict    — {sku, ordered, round_up_to, delta,
#                                   action}

from typing import Any, Dict


def round_up_moq(
    order_id: str,
    sku: str,
    ordered_qty: float,
    moq_qty: float,
    unit_cost: float,
    uom: str,
    severe_shortfall_pct: float,
    uplift_review_pct: float,
) -> Dict[str, Any]:
    if moq_qty <= 0 or ordered_qty < 0:
        return {
            "status": "FAILED",
            "classification": None,
            "shortfall_qty": None,
            "shortfall_pct": None,
            "uplift_qty": None,
            "uplift_pct": None,
            "uplift_value": None,
            "recommended_action": None,
            "round_up_plan": None,
            "order_id": order_id,
            "sku": sku,
            "reason": (
                f"moq_qty must be > 0 and ordered_qty >= 0; "
                f"got moq={moq_qty}, ordered={ordered_qty}."
            ),
        }

    if ordered_qty >= moq_qty:
        return {
            "status": "COMPLETE",
            "classification": "NO_SHORTFALL",
            "shortfall_qty": 0.0,
            "shortfall_pct": 0.0,
            "uplift_qty": 0.0,
            "uplift_pct": 0.0,
            "uplift_value": 0.0,
            "recommended_action": "NO_ACTION",
            "round_up_plan": None,
            "order_id": order_id,
            "sku": sku,
            "uom": uom,
        }

    shortfall_qty = moq_qty - ordered_qty
    shortfall_pct = shortfall_qty / moq_qty
    uplift_qty = shortfall_qty  # round-up to MOQ
    uplift_pct = uplift_qty / ordered_qty if ordered_qty > 0 else float("inf")
    uplift_value = round(uplift_qty * unit_cost, 2)

    # Classification is SEVERE if shortfall >= threshold. SEVERE always
    # escalates regardless of uplift size. MINOR cases may still
    # escalate if the uplift itself exceeds the review threshold.
    if shortfall_pct >= severe_shortfall_pct:
        classification = "SEVERE_SHORTFALL"
        recommended_action = "ESCALATE"
        status = "ESCALATE"
    elif ordered_qty > 0 and uplift_pct >= uplift_review_pct:
        classification = "MINOR_SHORTFALL"
        recommended_action = "ROUND_UP"
        status = "REVIEW_REQUIRED"
    else:
        classification = "MINOR_SHORTFALL"
        recommended_action = "ROUND_UP"
        status = "REVIEW_REQUIRED"

    plan_action = (
        "ESCALATE"
        if recommended_action == "ESCALATE"
        else "ROUND_UP"
    )
    round_up_plan = {
        "sku": sku,
        "ordered": ordered_qty,
        "round_up_to": moq_qty,
        "delta": round(uplift_qty, 3),
        "action": plan_action,
        "reason": (
            f"Ordered {ordered_qty} {uom} < MOQ {moq_qty} {uom} "
            f"(shortfall {shortfall_pct:.1%})."
        ),
    }

    return {
        "status": status,
        "classification": classification,
        "shortfall_qty": round(shortfall_qty, 3),
        "shortfall_pct": round(shortfall_pct, 4),
        "uplift_qty": round(uplift_qty, 3),
        "uplift_pct": round(uplift_pct, 4) if uplift_pct != float("inf") else None,
        "uplift_value": uplift_value,
        "recommended_action": recommended_action,
        "round_up_plan": round_up_plan,
        "order_id": order_id,
        "sku": sku,
        "uom": uom,
    }
