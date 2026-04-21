from __future__ import annotations

# Price Hold Release Recipe
#
# Deterministic release / escalate / hard-block decision for an EDI 850 line
# that is currently held because the PO price is outside tolerance of the
# SAP base price.
#
# This recipe is distinct from PriceAdjustmentRecipe.py. PriceAdjustment *changes*
# a price; this recipe *decides whether to lift an existing hold*. Both may
# dispatch against the same underlying price variance, but they are separate
# business actions with separate audit trails.
#
# Inputs:
#   order_id        : str        — host ERP order identifier
#   line_item       : int        — 1-based line number
#   po_price        : float      — price on the incoming PO
#   sap_base_price  : float      — authoritative base price in SAP
#   tolerance_pct   : float      — auto-release ceiling (|variance| <= tolerance)
#   hard_block_pct  : float      — hard-block floor   (|variance| >  hard_block)
#   hold_status     : str        — must equal "HELD"; other values return FAILED
#
# Outputs (Dict[str, Any]):
#   status        : "RELEASED" | "REVIEW_REQUIRED" | "REJECTED" | "FAILED"
#   action        : "AUTO_RELEASE" | "ESCALATE" | "HARD_BLOCK"
#   variance_pct  : float        — signed (po − sap) / sap
#   reason        : str          — human-readable reason (policy hit)
#   payload       : Dict         — OMS update payload for apply_effects
#
# Invariants:
#   - No LLM calls, no I/O, no side effects.
#   - sap_base_price must be > 0; ZeroDivision is raised otherwise (orchestration
#     catches it as FAIL_TO_HUMAN — invalid inbound data is a routing decision).
#   - tolerance_pct < hard_block_pct (asserted).

from typing import Any, Dict, Optional


def execute_price_hold_release(
    order_id: str,
    line_item: int,
    po_price: float,
    sap_base_price: float,
    tolerance_pct: float,
    hard_block_pct: float,
    hold_status: str,
    requester_role: Optional[str] = None,
) -> Dict[str, Any]:
    """Decide the disposition of a held EDI 850 order line.

    Three branches (closed inequalities on the lower bound):
      |variance| <= tolerance_pct                    → AUTO_RELEASE (RELEASED)
      tolerance_pct < |variance| <= hard_block_pct   → ESCALATE     (REVIEW_REQUIRED)
      |variance| >  hard_block_pct                   → HARD_BLOCK   (REJECTED)

    Args:
        order_id:        Host ERP order identifier.
        line_item:       1-based line number on the order.
        po_price:        Price on the incoming PO (PRICE segment).
        sap_base_price:  SAP base (list) price for the sku on this day.
        tolerance_pct:   Auto-release variance ceiling (decimal fraction).
        hard_block_pct:  Hard-block variance floor (decimal fraction).
        hold_status:     Must equal "HELD"; anything else returns status=FAILED.
        requester_role:  Informational only — logged for audit, no gating.

    Returns:
        Dict with keys: status, action, variance_pct, reason, payload.
    """
    assert tolerance_pct < hard_block_pct, (
        "tolerance_pct must be strictly less than hard_block_pct"
    )

    if hold_status != "HELD":
        return {
            "status": "FAILED",
            "action": None,
            "variance_pct": None,
            "reason": f"hold_status must be HELD, got {hold_status!r}",
            "payload": None,
        }

    variance_pct = (po_price - sap_base_price) / sap_base_price
    abs_variance = abs(variance_pct)

    if abs_variance <= tolerance_pct:
        action = "AUTO_RELEASE"
        status = "RELEASED"
        reason = (
            f"Variance {variance_pct:.4f} within tolerance "
            f"{tolerance_pct:.4f}; auto-release."
        )
    elif abs_variance <= hard_block_pct:
        action = "ESCALATE"
        status = "REVIEW_REQUIRED"
        reason = (
            f"Variance {variance_pct:.4f} above tolerance {tolerance_pct:.4f} "
            f"but within hard-block {hard_block_pct:.4f}; manual review required."
        )
    else:
        action = "HARD_BLOCK"
        status = "REJECTED"
        reason = (
            f"Variance {variance_pct:.4f} exceeds hard-block "
            f"{hard_block_pct:.4f}; order rejected."
        )

    payload = {
        "OrderID": order_id,
        "Line": line_item,
        "Action": action,
        "VariancePct": round(variance_pct, 6),
        "RequesterRole": requester_role,
    }

    return {
        "status": status,
        "action": action,
        "variance_pct": round(variance_pct, 6),
        "reason": reason,
        "payload": payload,
        "order_id": order_id,
    }
