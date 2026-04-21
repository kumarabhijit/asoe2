from __future__ import annotations

# EDI 850 Line Mismatch Recipe
#
# Deterministic classification of a non-duplicate EDI 850 line mismatch.
# PRICE_MISMATCH is deliberately absent from the accepted sub_type vocabulary —
# those events are routed to CONTRACTUAL_CORRECTION / PriceAdjustmentRecipe.py
# at classifier time so pricing retains a single source of truth.
#
# Inputs:
#   order_id         : str    — host ERP order identifier
#   sub_type         : str    — one of SKU_MISMATCH | QTY_MISMATCH |
#                               UOM_MISMATCH | SHIP_TO_MISMATCH. Any other
#                               value returns status=FAILED.
#   expected_value   : Any    — value on the master record (sku, qty, uom, ship-to)
#   received_value   : Any    — value received in the EDI 850
#   autonomy_levels  : Dict[str, str]  — sub_type → autonomy level (L1/L2/L3)
#
# Outputs (Dict[str, Any]):
#   status             : "REJECTED" | "REVIEW_REQUIRED" | "FAILED"
#   sub_type           : echoed input
#   classification     : "HARD_REJECT" | "REVIEW" | "ESCALATE"
#   recommended_action : "BLOCK_AND_NOTIFY" | "REQUEST_BUYER_CONFIRMATION" | "ESCALATE"
#   autonomy_level     : str  — resolved from autonomy_levels mapping
#   notification_template : Optional[str]
#   expected_value     : echoed input
#   received_value     : echoed input
#
# Invariants:
#   - No LLM calls, no I/O, no side effects.
#   - sub_type vocabulary is closed; unknown values return FAILED so the
#     orchestration layer routes to FAIL_TO_HUMAN.

from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Sub-type → classification / action mapping.
# PRICE_MISMATCH is intentionally NOT listed — handled upstream by the
# classifier routing to CONTRACTUAL_CORRECTION / PriceAdjustmentRecipe.py.
# ---------------------------------------------------------------------------

_CLASSIFICATION_BY_SUB_TYPE: Dict[str, str] = {
    "SKU_MISMATCH":     "HARD_REJECT",
    "QTY_MISMATCH":     "REVIEW",
    "UOM_MISMATCH":     "REVIEW",
    "SHIP_TO_MISMATCH": "ESCALATE",
}

_STATUS_BY_CLASSIFICATION: Dict[str, str] = {
    "HARD_REJECT": "REJECTED",
    "REVIEW":      "REVIEW_REQUIRED",
    "ESCALATE":    "REVIEW_REQUIRED",
}

_ACTION_BY_CLASSIFICATION: Dict[str, str] = {
    "HARD_REJECT": "BLOCK_AND_NOTIFY",
    "REVIEW":      "REQUEST_BUYER_CONFIRMATION",
    "ESCALATE":    "ESCALATE",
}

_NOTIFICATION_TEMPLATES: Dict[str, Optional[str]] = {
    "BLOCK_AND_NOTIFY":           "edi_line_mismatch_blocked",
    "REQUEST_BUYER_CONFIRMATION": "edi_line_mismatch_inquiry",
    "ESCALATE":                   None,
}


def detect_edi_mismatch(
    order_id: str,
    sub_type: str,
    expected_value: Any,
    received_value: Any,
    autonomy_levels: Dict[str, str],
) -> Dict[str, Any]:
    """Classify an EDI 850 line mismatch by sub_type.

    Args:
        order_id:         Host ERP order identifier.
        sub_type:         One of the members of _CLASSIFICATION_BY_SUB_TYPE.
                          Any other value produces status=FAILED so the
                          orchestration layer can route to FAIL_TO_HUMAN.
        expected_value:   Authoritative value (from master data).
        received_value:   Value received in the EDI 850 line.
        autonomy_levels:  Sub_type → autonomy level mapping (policy-injected).

    Returns:
        Dict with keys: status, sub_type, classification, recommended_action,
        autonomy_level, notification_template, expected_value, received_value,
        order_id.
    """
    classification = _CLASSIFICATION_BY_SUB_TYPE.get(sub_type)

    if classification is None:
        return {
            "status": "FAILED",
            "sub_type": sub_type,
            "classification": None,
            "recommended_action": None,
            "autonomy_level": None,
            "notification_template": None,
            "expected_value": expected_value,
            "received_value": received_value,
            "order_id": order_id,
            "reason": (
                f"sub_type {sub_type!r} is not a recognised EDI mismatch "
                f"sub_type; expected one of "
                f"{sorted(_CLASSIFICATION_BY_SUB_TYPE)}"
            ),
        }

    status = _STATUS_BY_CLASSIFICATION[classification]
    recommended_action = _ACTION_BY_CLASSIFICATION[classification]
    autonomy_level = autonomy_levels.get(sub_type)

    return {
        "status": status,
        "sub_type": sub_type,
        "classification": classification,
        "recommended_action": recommended_action,
        "autonomy_level": autonomy_level,
        "notification_template": _NOTIFICATION_TEMPLATES.get(recommended_action),
        "expected_value": expected_value,
        "received_value": received_value,
        "order_id": order_id,
    }
