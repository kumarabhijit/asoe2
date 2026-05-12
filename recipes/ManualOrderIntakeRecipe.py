from __future__ import annotations

# Manual Order Intake Recipe (formerly Email Order Entry — see ADR-034 §6.3)
#
# Deterministic classification of a non-EDI email-channel sales order on the
# email-intake leg of the order-to-cash pipeline. Inputs are the *post-extraction*
# envelope — composite confidence, validation failures, and four "non-disable-able
# floor" check results (sender authorisation, customer identity resolution,
# duplicate PO pre-check, credit block pre-check). The recipe never sees the raw
# email body, attachments, or any free-text content; the upstream
# email-intelligence-agent + extraction gateways produce the envelope before the
# graph runs.
#
# Reference spec: knowledge/skills/manual-order-intake/specs/order_entry_spec.md (§3, §4, §5).
# Bucketed mapping: docs/adr/ADR-034-email-order-entry-skill.md.
#
# Inputs:
#   order_id                       : str               — host trace / order identifier
#   customer_id                    : str               — resolved sold-to identifier
#                                                        (may be empty when the
#                                                        customer-identity floor
#                                                        check failed)
#   composite_confidence           : float             — overall extraction +
#                                                        resolution confidence
#                                                        in [0.0, 1.0]
#   validation_failures            : List[str]         — typed failure codes from
#                                                        the validation suite
#                                                        (CHEAP + EXPENSIVE).
#                                                        Empty list means "all
#                                                        validations passed".
#   non_disableable_floor          : Dict[str, bool]   — must contain keys
#                                                          sender_authorized
#                                                          customer_resolved
#                                                          duplicate_po_clear
#                                                          credit_clear
#                                                        Missing keys default to
#                                                        False (conservative).
#   threshold_auto_approve         : float             — composite_confidence at
#                                                        or above which a record
#                                                        is one-click eligible.
#                                                        Default 0.95 per spec.
#   threshold_review_band_low      : float             — lower bound of the
#                                                        review band. Default 0.85
#                                                        per spec.
#   threshold_auto_correct         : float             — composite_confidence at
#                                                        or above which AUTO_CORRECT
#                                                        may be applied. Default
#                                                        0.99 per spec.
#   autonomy_levels                : Dict[str, str]    — recommended_action →
#                                                        L1/L2/L3/L4 mapping.
#                                                        Injected from policy.
#   reject_reason_code             : Optional[str]     — when supplied, the recipe
#                                                        treats this as a fatal
#                                                        terminal close (e.g.
#                                                        sender_unauthorized,
#                                                        corrupt_input). Validated
#                                                        against a closed set.
#
# Outputs (Dict[str, Any]):
#   status               : "REJECTED" | "REVIEW_REQUIRED" | "SUCCESS"
#   classification       : "ONE_CLICK_APPROVE" | "STANDARD_REVIEW" |
#                          "LOW_CONFIDENCE" | "FATAL_REJECT"
#   recommended_action   : a value from AllowedResolutionAction
#   autonomy_level       : str   — resolved from autonomy_levels mapping
#   notification_template: Optional[str]
#   composite_confidence : float — echoed
#   validation_failures  : List[str] — echoed
#   floor_breaches       : List[str] — names of floor checks that failed
#   reject_reason_code   : Optional[str] — populated when classification is
#                          FATAL_REJECT
#
# Invariants:
#   - Pure function: no I/O, no LLM calls, no side effects.
#   - All threshold comparisons are closed on the lower bound.
#   - The "non-disable-able floor" rule is policy that may not be tenant-overridden
#     and is enforced inside the recipe (defence in depth — gateway failure
#     should already have halted the graph; if a misconfiguration lets the run
#     through, this branch still rejects).
#   - L4 is demoted to L3 by the caller (policy.py mapping) until calibration
#     ships graduation signals (ADR-032). This recipe does not enforce the
#     demotion — it consumes whatever the policy mapping resolves.

from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Closed vocabularies — sourced from policy / spec.
# Kept as module-level tuples so unit tests can import and assert against
# them rather than copy/pasting.
# ---------------------------------------------------------------------------

FLOOR_KEYS: tuple[str, ...] = (
    "sender_authorized",
    "customer_resolved",
    "duplicate_po_clear",
    "credit_clear",
)

ALLOWED_REJECT_REASONS: tuple[str, ...] = (
    "sender_unauthorized",
    "customer_unresolved",
    "duplicate_po_confirmed",
    "credit_block",
    "corrupt_input",
    "policy_floor_breach",
)

# ---------------------------------------------------------------------------
# classification → (status, recommended_action) defaults
# ---------------------------------------------------------------------------

_DEFAULT_STATUS_BY_CLASSIFICATION: Dict[str, str] = {
    "ONE_CLICK_APPROVE": "SUCCESS",
    "STANDARD_REVIEW":   "REVIEW_REQUIRED",
    "LOW_CONFIDENCE":    "REVIEW_REQUIRED",
    "FATAL_REJECT":      "REJECTED",
}

_DEFAULT_ACTION_BY_CLASSIFICATION: Dict[str, str] = {
    "ONE_CLICK_APPROVE": "ONE_CLICK_APPROVE",
    "STANDARD_REVIEW":   "STANDARD_REVIEW",
    "LOW_CONFIDENCE":    "LOW_CONFIDENCE_FLAG",
    "FATAL_REJECT":      "REJECT",
}

# Notification template per recommended action. None means no buyer notification.
_NOTIFICATION_TEMPLATES: Dict[str, Optional[str]] = {
    "ONE_CLICK_APPROVE":     None,
    "STANDARD_REVIEW":       None,
    "LOW_CONFIDENCE_FLAG":   None,
    "AUTO_CORRECT":          "email_order_auto_corrected",
    "REQUEST_CLARIFICATION": "email_order_clarification_request",
    "REJECT":                "email_order_rejected",
    "ESCALATE":              None,
}

# Validation-failure codes that are *recoverable via deterministic auto-correct*
# when composite_confidence is at or above the auto-correct threshold. Anything
# outside this set means the agent must escalate to human review even at very
# high confidence (the failure is structural, not transcriptional).
_AUTO_CORRECTABLE_FAILURES: frozenset[str] = frozenset({
    "uom_normalisation_required",
    "ship_to_address_format",
    "po_number_padding",
})

# Validation-failure codes that demand a customer round-trip rather than
# internal escalation (REQUEST_CLARIFICATION instead of ESCALATE).
_CUSTOMER_CLARIFICATION_FAILURES: frozenset[str] = frozenset({
    "missing_delivery_date",
    "ambiguous_ship_to",
    "missing_po_number",
})


def classify_manual_order_intake(
    order_id: str,
    customer_id: str,
    composite_confidence: float,
    validation_failures: List[str],
    non_disableable_floor: Dict[str, bool],
    autonomy_levels: Dict[str, str],
    threshold_auto_approve: float = 0.95,
    threshold_review_band_low: float = 0.85,
    threshold_auto_correct: float = 0.99,
    reject_reason_code: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify a post-extraction email-channel order envelope.

    The decision tree (in priority order):
      1. If a fatal `reject_reason_code` is supplied → FATAL_REJECT / REJECT.
      2. If any of the four non-disable-able floor checks failed →
         FATAL_REJECT / REJECT, with a derived reject_reason_code.
      3. If validation_failures contains any clarification-class failure →
         REVIEW_REQUIRED / REQUEST_CLARIFICATION (regardless of confidence).
      4. If composite_confidence >= threshold_auto_correct AND every
         validation failure is in the auto-correctable set →
         REVIEW_REQUIRED / AUTO_CORRECT.
      5. If composite_confidence >= threshold_auto_approve AND
         validation_failures is empty → SUCCESS / ONE_CLICK_APPROVE.
      6. If composite_confidence >= threshold_review_band_low →
         REVIEW_REQUIRED / STANDARD_REVIEW.
      7. Else → REVIEW_REQUIRED / LOW_CONFIDENCE_FLAG.

    Args:
        order_id:                 Host trace / order identifier.
        customer_id:              Resolved sold-to id (empty when unresolved).
        composite_confidence:     Overall extraction + resolution score in
                                  [0.0, 1.0]. Out-of-range values are
                                  clamped before comparison; the echo field
                                  carries the raw value.
        validation_failures:      Typed failure codes; empty means "all passed".
        non_disableable_floor:    Floor-check results keyed by FLOOR_KEYS.
                                  Missing keys default to False.
        autonomy_levels:          recommended_action → L1/L2/L3/L4 mapping.
        threshold_auto_approve:   Closed lower bound of the auto-approve band.
        threshold_review_band_low: Closed lower bound of the standard-review band.
        threshold_auto_correct:   Closed lower bound of the auto-correct band.
        reject_reason_code:       Optional fatal terminator. When supplied,
                                  must be a member of ALLOWED_REJECT_REASONS.

    Returns:
        Dict with the keys documented at the top of this module.
    """
    # --- Input sanitisation --------------------------------------------------

    # Defensive clamp — preserves the raw value in the echo for audit.
    confidence_for_decision = max(0.0, min(1.0, float(composite_confidence)))

    floor_breaches: List[str] = []
    for key in FLOOR_KEYS:
        if not bool(non_disableable_floor.get(key, False)):
            floor_breaches.append(key)

    failures: List[str] = list(validation_failures or [])

    # --- Decision tree -------------------------------------------------------

    classification: str
    recommended_action: str
    resolved_reject_reason: Optional[str] = None

    if reject_reason_code is not None:
        if reject_reason_code not in ALLOWED_REJECT_REASONS:
            # Defensive: an unknown reject_reason_code is itself a routing
            # invariant break; treat it as corrupt_input rather than letting
            # an unconstrained string flow through to the audit payload.
            resolved_reject_reason = "corrupt_input"
        else:
            resolved_reject_reason = reject_reason_code
        classification = "FATAL_REJECT"
        recommended_action = "REJECT"
    elif floor_breaches:
        classification = "FATAL_REJECT"
        recommended_action = "REJECT"
        # Map the *first* breached floor to a typed reason so the audit
        # payload has a single primary cause; the full list is preserved
        # in floor_breaches.
        resolved_reject_reason = _floor_breach_reason(floor_breaches[0])
    elif _has_clarification_failure(failures):
        classification = "STANDARD_REVIEW"
        recommended_action = "REQUEST_CLARIFICATION"
    elif (
        confidence_for_decision >= threshold_auto_correct
        and failures
        and all(f in _AUTO_CORRECTABLE_FAILURES for f in failures)
    ):
        classification = "STANDARD_REVIEW"
        recommended_action = "AUTO_CORRECT"
    elif confidence_for_decision >= threshold_auto_approve and not failures:
        classification = "ONE_CLICK_APPROVE"
        recommended_action = "ONE_CLICK_APPROVE"
    elif confidence_for_decision >= threshold_review_band_low:
        classification = "STANDARD_REVIEW"
        recommended_action = "STANDARD_REVIEW"
    else:
        classification = "LOW_CONFIDENCE"
        recommended_action = "LOW_CONFIDENCE_FLAG"

    # If validation_failures contains anything that is *not* auto-correctable
    # and *not* clarification-class while we landed in REVIEW_REQUIRED/
    # STANDARD_REVIEW with action STANDARD_REVIEW, escalate the action to
    # ESCALATE. This is the spec's "agent proposes / human decides" path
    # collapsing into the review band when the failure is internal-team
    # territory (finance, pricing, supply).
    if recommended_action == "STANDARD_REVIEW" and failures and not all(
        (f in _AUTO_CORRECTABLE_FAILURES) or (f in _CUSTOMER_CLARIFICATION_FAILURES)
        for f in failures
    ):
        recommended_action = "ESCALATE"

    status = _DEFAULT_STATUS_BY_CLASSIFICATION[classification]

    return {
        "status": status,
        "classification": classification,
        "recommended_action": recommended_action,
        "autonomy_level": autonomy_levels.get(recommended_action),
        "notification_template": _NOTIFICATION_TEMPLATES.get(recommended_action),
        "composite_confidence": float(composite_confidence),
        "validation_failures": failures,
        "floor_breaches": floor_breaches,
        "reject_reason_code": resolved_reject_reason,
        "order_id": order_id,
        "customer_id": customer_id,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_clarification_failure(failures: List[str]) -> bool:
    return any(f in _CUSTOMER_CLARIFICATION_FAILURES for f in failures)


def _floor_breach_reason(floor_key: str) -> str:
    return {
        "sender_authorized":  "sender_unauthorized",
        "customer_resolved":  "customer_unresolved",
        "duplicate_po_clear": "duplicate_po_confirmed",
        "credit_clear":       "credit_block",
    }.get(floor_key, "policy_floor_breach")
