from __future__ import annotations

# Duplicate PO Detection Recipe
#
# Deterministic scoring and classification of a potential duplicate
# Purchase Order (PO) against a set of pre-computed per-signal match scores.
#
# Inputs:
#   incoming_po_number : str              — normalized incoming PO number
#   customer_id        : str              — sold-to / bill-to party identifier
#   signal_scores      : Dict[str, float] — per-signal match scores in [0.0, 1.0]
#                        Expected keys: po_number, customer_id, line_items,
#                        amount, timestamp, ship_to, channel, delivery_date.
#                        Missing signals default to 0.0.
#
# Outputs (Dict[str, Any]):
#   status             : "BLOCKED" | "REVIEW_REQUIRED" | "SOFT_FLAG" | "PASS"
#   composite_score    : float            — weighted aggregate of signal scores
#   classification     : str             — mirrors status
#   recommended_action : str             — deterministic action for this class
#   signal_breakdown   : Dict[str, float] — weighted contribution per signal
#
# Invariants:
#   - Weights sum to 1.0 (validated at module load time via assertion).
#   - All threshold comparisons are closed on the lower bound.
#   - No LLM calls, no I/O, no side effects.
#   - MASS_PRICING_ERROR intent routes to FAIL_TO_HUMAN upstream; this recipe
#     is invoked only for DUPLICATE_PO intent.

from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Signal weights — sourced from the product specification.
# Must sum to 1.0.  These are algorithmic, not policy thresholds.
# ---------------------------------------------------------------------------

_WEIGHTS: Dict[str, float] = {
    "po_number":     0.30,
    "customer_id":   0.15,
    "line_items":    0.20,
    "amount":        0.10,
    "timestamp":     0.10,
    "ship_to":       0.05,
    "channel":       0.05,
    "delivery_date": 0.05,
}

assert abs(sum(_WEIGHTS.values()) - 1.0) < 1e-9, "Signal weights must sum to 1.0"

# ---------------------------------------------------------------------------
# Action mapping — one deterministic action per classification
# ---------------------------------------------------------------------------

# Default action per classification — used when no resolution context is
# available.  When resolution_context is provided (Phase C), the decision
# tree refines the action within each classification tier.
_DEFAULT_ACTIONS: Dict[str, str] = {
    "AUTO_BLOCK":       "BLOCK_AND_NOTIFY",
    "REVIEW_REQUIRED":  "ESCALATE",
    "SOFT_FLAG":        "REQUEST_BUYER_CONFIRMATION",
    "PASS":             "ALLOW_BOTH",
}


def detect_duplicate_po(
    incoming_po_number: str,
    customer_id: str,
    signal_scores: Dict[str, float],
    threshold_auto_block: float = 0.90,
    threshold_review_required: float = 0.70,
    threshold_soft_flag: float = 0.50,
    # Resolution context — resolved by gateway dependencies (Phase B).
    # When None, the recipe falls back to the default action per classification.
    # When provided, the decision tree (Phase C) refines the action.
    original_fulfilled: Optional[bool] = None,
    has_revision_indicator: Optional[bool] = None,
    line_items_identical: Optional[bool] = None,
    # Autonomy level mapping — injected from policy (Phase D).
    # Maps resolution action → autonomy level string (L1–L4).
    autonomy_levels: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Score and classify an incoming PO against pre-computed signal scores.

    Classification thresholds are injected by the orchestration layer so the
    same logic can serve different customer / vendor threshold sets.

    Args:
        incoming_po_number:        Normalized PO number of the incoming order.
        customer_id:               Sold-to / bill-to party identifier.
        signal_scores:             Per-signal match scores in [0.0, 1.0].
                                   Keys must match those in _WEIGHTS; missing
                                   keys default to 0.0 (conservative).
        threshold_auto_block:      Score >= this → AUTO_BLOCK.
        threshold_review_required: Score >= this → REVIEW_REQUIRED.
        threshold_soft_flag:       Score >= this → SOFT_FLAG.

    Returns:
        Dict with keys: status, composite_score, classification,
        recommended_action, signal_breakdown.
    """
    # Compute weighted contribution for each signal.
    breakdown: Dict[str, float] = {
        signal: round(_WEIGHTS[signal] * float(signal_scores.get(signal, 0.0)), 6)
        for signal in _WEIGHTS
    }

    composite_score = round(sum(breakdown.values()), 6)

    # Classify using closed lower-bound thresholds.
    if composite_score >= threshold_auto_block:
        classification = "AUTO_BLOCK"
        status = "BLOCKED"
    elif composite_score >= threshold_review_required:
        classification = "REVIEW_REQUIRED"
        status = "REVIEW_REQUIRED"
    elif composite_score >= threshold_soft_flag:
        classification = "SOFT_FLAG"
        status = "SOFT_FLAG"
    else:
        classification = "PASS"
        status = "PASS"

    # Determine recommended action via decision tree when resolution
    # context is available, otherwise fall back to default mapping.
    recommended_action = _resolve_action(
        classification,
        original_fulfilled=original_fulfilled,
        has_revision_indicator=has_revision_indicator,
        line_items_identical=line_items_identical,
    )

    # Resolve autonomy level from policy mapping when provided.
    autonomy_level = None
    if autonomy_levels:
        autonomy_level = autonomy_levels.get(recommended_action)

    return {
        "status": status,
        "composite_score": composite_score,
        "classification": classification,
        "recommended_action": recommended_action,
        "autonomy_level": autonomy_level,
        "signal_breakdown": breakdown,
        "incoming_po_number": incoming_po_number,
        "customer_id": customer_id,
    }


def _resolve_action(
    classification: str,
    *,
    original_fulfilled: Optional[bool],
    has_revision_indicator: Optional[bool],
    line_items_identical: Optional[bool],
) -> str:
    """Decision tree mapping (classification + resolution context) → action.

    When all resolution context fields are None (no gateway data), falls back
    to the default action for the classification tier.

    Decision tree (spec §3.2):
      AUTO_BLOCK:
        identical lines + not fulfilled   → BLOCK_AND_NOTIFY  (true duplicate)
        identical lines + fulfilled       → ALLOW_BOTH        (likely reorder)
        revision indicator present        → SUPERSEDE
        lines differ, no revision         → MERGE
      REVIEW_REQUIRED:
        revision indicator present        → SUPERSEDE
        identical lines                   → ESCALATE
        lines differ                      → REQUEST_BUYER_CONFIRMATION
      SOFT_FLAG / PASS: default action (low confidence, context doesn't refine)
    """
    has_context = any(v is not None for v in (
        original_fulfilled, has_revision_indicator, line_items_identical,
    ))

    if not has_context:
        return _DEFAULT_ACTIONS[classification]

    if classification == "AUTO_BLOCK":
        if line_items_identical and not original_fulfilled:
            return "BLOCK_AND_NOTIFY"
        if line_items_identical and original_fulfilled:
            return "ALLOW_BOTH"
        if has_revision_indicator:
            return "SUPERSEDE"
        return "MERGE"

    if classification == "REVIEW_REQUIRED":
        if has_revision_indicator:
            return "SUPERSEDE"
        if line_items_identical:
            return "ESCALATE"
        return "REQUEST_BUYER_CONFIRMATION"

    return _DEFAULT_ACTIONS[classification]
