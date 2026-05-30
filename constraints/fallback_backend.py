from __future__ import annotations

from typing import Optional

from contracts.models import GraphState, Intent
from contracts.policy import (
    BACK_ORDER_SEVERE_GAP_PCT,
    CIRCUIT_BREAKER_MAX_VARIANCE,
    DELIVERY_DELAY_MINOR_DAYS,
    DELIVERY_DELAY_SEVERE_DAYS,
    MASS_UPDATE_LINE_COUNT_THRESHOLD,
    MOQ_SEVERE_SHORTFALL_PCT,
    OVER_MAX_SEVERE_EXCEEDANCE_PCT,
    PALLET_CONFIG_MIN_FILL_PCT,
    PRICE_HOLD_HARD_BLOCK_PCT,
    PRICE_HOLD_TOLERANCE_PCT,
)
from constraints.specs import (
    AllowedCustomerSupergroup,
    EmailSupergroupDecision,
    IntentDecision,
    RecipeProposal,
    ShadowDecisionSchema,
)

# ADR-036 — the upstream producer / email-intelligence stand-in stamps a
# proposed CUSTOMER supergroup on event.metadata under this key. The
# deterministic backend relays it (constrained to AllowedCustomerSupergroup);
# the live RemoteLLMBackend replaces the hint with real classification.
EMAIL_SUPERGROUP_HINT_KEY = "email_supergroup_hint"

_CUSTOMER_SUPERGROUPS: frozenset[str] = frozenset(
    AllowedCustomerSupergroup.__args__  # type: ignore[attr-defined]
)


class DeterministicFallbackBackend:
    def classify_email_supergroup(self, state: GraphState) -> EmailSupergroupDecision:
        """ADR-036 D5 bootstrap. Relays the producer's supergroup hint as a
        high-confidence decision; a missing/unknown hint yields a low-
        confidence SG_NEEDS_TRIAGE so the threshold sink is exercised
        honestly. Pure code — no prompt-injection surface, always available.
        This is the net the router falls closed to for the email_supergroup
        task when no model is configured (local / CI / Vercel preview)."""
        meta = (state.event.metadata or {}) if state.event else {}
        hint = meta.get(EMAIL_SUPERGROUP_HINT_KEY)
        if hint in _CUSTOMER_SUPERGROUPS and hint != "SG_NEEDS_TRIAGE":
            return EmailSupergroupDecision(
                supergroup_code=hint,
                confidence=0.95,
                rationale=f"deterministic backend: producer hint {hint!r}",
            )
        return EmailSupergroupDecision(
            supergroup_code="SG_NEEDS_TRIAGE",
            confidence=0.0,
            rationale="no usable supergroup hint — routed to triage",
        )

    def classify_intent(self, state: GraphState) -> IntentDecision:
        if state.event.line_count > MASS_UPDATE_LINE_COUNT_THRESHOLD:
            return IntentDecision(intent="MASS_PRICING_ERROR", confidence=0.95, rationale=f"line_count > {MASS_UPDATE_LINE_COUNT_THRESHOLD}")
        if state.event.event_type in ("EDI_850_DUPLICATE_PO", "DUPLICATE"):
            return IntentDecision(intent="DUPLICATE_PO", confidence=0.90, rationale="duplicate PO event type")
        if state.event.event_type == "EDI_850_PRICE_HOLD":
            return IntentDecision(
                intent="PRICE_HOLD_RELEASE",
                confidence=0.95,
                rationale="price-hold event type",
            )
        if state.event.event_type == "EDI_850_LINE_MISMATCH":
            # PRICE_MISMATCH sub_type routes to the pricing path — preserves
            # PriceAdjustmentRecipe as the single source of truth for price
            # corrections. All other sub_types route to EDI_MISMATCH.
            sub_type = state.event.metadata.get("mismatch_sub_type")
            if sub_type == "PRICE_MISMATCH":
                return IntentDecision(
                    intent="CONTRACTUAL_CORRECTION",
                    confidence=0.90,
                    rationale="EDI line mismatch on price → routed to pricing path",
                )
            return IntentDecision(
                intent="EDI_MISMATCH",
                confidence=0.90,
                rationale=f"EDI line mismatch sub_type={sub_type}",
            )
        if state.event.event_type == "BACK_ORDER_OOS":
            return IntentDecision(
                intent="BACK_ORDER",
                confidence=0.95,
                rationale="back-order / OOS event type",
            )
        if state.event.event_type == "OVER_MAX_QTY":
            return IntentDecision(
                intent="OVER_MAX",
                confidence=0.95,
                rationale="over-max quantity event type",
            )
        if state.event.event_type == "MIN_ORDER_QTY":
            return IntentDecision(
                intent="MIN_ORDER_QTY",
                confidence=0.95,
                rationale="minimum-order-quantity event type",
            )
        if state.event.event_type == "PALLET_CONFIG_VIOLATION":
            return IntentDecision(
                intent="PALLET_CONFIG",
                confidence=0.95,
                rationale="pallet-configuration violation event type",
            )
        if state.event.event_type == "DELIVERY_DELAY":
            return IntentDecision(
                intent="DELIVERY_DELAY",
                confidence=0.95,
                rationale="delivery-delay event type",
            )
        if state.event.event_type in (
            "EMAIL_ORDER_ENTRY_REQUEST", "EMAIL_ORDER", "MANUAL_ORDER_INTAKE",
        ):
            return IntentDecision(
                intent="MANUAL_ORDER_INTAKE",
                confidence=0.95,
                rationale="email-channel order intake event type",
            )
        if state.event.requester_role and state.event.credit_limit is not None and state.event.current_exposure is not None:
            return IntentDecision(intent="CREDIT_BLOCK", confidence=0.80, rationale="credit fields present")
        return IntentDecision(intent="CONTRACTUAL_CORRECTION", confidence=0.90, rationale="within pricing path")

    def propose_recipe(self, state: GraphState) -> Optional[RecipeProposal]:
        mapping = {
            Intent.CONTRACTUAL_CORRECTION: "PriceAdjustmentRecipe.py",
            Intent.CREDIT_BLOCK: "CreditHoldReleaseRecipe.py",
            Intent.DUPLICATE_PO: "DuplicatePORecipe.py",
            Intent.PRICE_HOLD_RELEASE: "PriceHoldReleaseRecipe.py",
            Intent.EDI_MISMATCH: "EdiMismatchRecipe.py",
            Intent.BACK_ORDER: "BackOrderResolutionRecipe.py",
            Intent.OVER_MAX: "OverMaxTrimRecipe.py",
            Intent.MIN_ORDER_QTY: "MOQRoundUpRecipe.py",
            Intent.PALLET_CONFIG: "PalletAlignmentRecipe.py",
            Intent.DELIVERY_DELAY: "DeliveryDelayResolutionRecipe.py",
            # ADR-034 §6.3 — canonical recipe name post-cutover.
            Intent.MANUAL_ORDER_INTAKE: "ManualOrderIntakeRecipe.py",
            Intent.MASS_PRICING_ERROR: None,
        }
        recipe_name = mapping.get(state.intent)
        return None if recipe_name is None else RecipeProposal(recipe_name=recipe_name)

    def shadow_decision(self, state: GraphState) -> ShadowDecisionSchema:
        if state.intent == Intent.MASS_PRICING_ERROR:
            return ShadowDecisionSchema(
                status="RED",
                reasons=["Systemic pricing failure requires human escalation."],
                policy_hits=["HITL_REQUIRED_FOR_SYSTEMIC_FAILURE"],
            )
        if state.batch_total_variance > CIRCUIT_BREAKER_MAX_VARIANCE:
            return ShadowDecisionSchema(
                status="RED",
                reasons=["Circuit breaker threshold exceeded for total batch variance."],
                policy_hits=["CIRCUIT_BREAKER_VARIANCE"],
            )
        if state.event.line_count > MASS_UPDATE_LINE_COUNT_THRESHOLD:
            return ShadowDecisionSchema(
                status="RED",
                reasons=[f">{MASS_UPDATE_LINE_COUNT_THRESHOLD} line items suggests mass update risk."],
                policy_hits=["MASS_UPDATE_DETECTED"],
            )
        if state.intent == Intent.CREDIT_BLOCK:
            return ShadowDecisionSchema(
                status="YELLOW",
                reasons=["Credit path requires manual review by policy."],
                policy_hits=["CREDIT_RELEASE_REVIEW"],
            )
        if state.intent == Intent.PRICE_HOLD_RELEASE:
            return _shadow_for_price_hold_release(state)
        if state.intent == Intent.EDI_MISMATCH:
            return _shadow_for_edi_mismatch(state)
        if state.intent == Intent.BACK_ORDER:
            return _shadow_for_back_order(state)
        if state.intent == Intent.OVER_MAX:
            return _shadow_for_over_max(state)
        if state.intent == Intent.MIN_ORDER_QTY:
            return _shadow_for_moq(state)
        if state.intent == Intent.PALLET_CONFIG:
            return _shadow_for_pallet_config(state)
        if state.intent == Intent.DELIVERY_DELAY:
            return _shadow_for_delivery_delay(state)
        return ShadowDecisionSchema(
            status="GREEN",
            reasons=["No blocking policy hit detected."],
            policy_hits=[],
        )


def _shadow_for_price_hold_release(state: GraphState) -> ShadowDecisionSchema:
    """Shadow verdict for PRICE_HOLD_RELEASE based on |variance| vs policy."""
    base = state.event.sap_base_price
    po = state.event.po_price
    if base <= 0:
        return ShadowDecisionSchema(
            status="RED",
            reasons=["Invalid SAP base price; cannot compute variance."],
            policy_hits=["PRICE_HOLD_INVALID_BASE"],
        )
    abs_variance = abs((po - base) / base)
    if abs_variance > PRICE_HOLD_HARD_BLOCK_PCT:
        return ShadowDecisionSchema(
            status="RED",
            reasons=[
                f"Variance {abs_variance:.4f} exceeds hard-block threshold "
                f"{PRICE_HOLD_HARD_BLOCK_PCT:.4f}."
            ],
            policy_hits=["PRICE_HOLD_HARD_BLOCK"],
        )
    if abs_variance > PRICE_HOLD_TOLERANCE_PCT:
        return ShadowDecisionSchema(
            status="YELLOW",
            reasons=[
                f"Variance {abs_variance:.4f} exceeds tolerance "
                f"{PRICE_HOLD_TOLERANCE_PCT:.4f}; manual review required."
            ],
            policy_hits=["PRICE_HOLD_TOLERANCE_ESCALATE"],
        )
    return ShadowDecisionSchema(
        status="GREEN",
        reasons=["Variance within tolerance; auto-release permitted."],
        policy_hits=["PRICE_HOLD_TOLERANCE_OK"],
    )


def _shadow_for_edi_mismatch(state: GraphState) -> ShadowDecisionSchema:
    """Shadow verdict for EDI_MISMATCH based on metadata.mismatch_sub_type."""
    sub_type = state.event.metadata.get("mismatch_sub_type")
    if sub_type == "SKU_MISMATCH":
        return ShadowDecisionSchema(
            status="RED",
            reasons=["SKU mismatch — order cannot fulfil as received."],
            policy_hits=["EDI_SKU_MISMATCH_HARD_REJECT"],
        )
    if sub_type == "SHIP_TO_MISMATCH":
        return ShadowDecisionSchema(
            status="YELLOW",
            reasons=["Ship-to mismatch — escalation required."],
            policy_hits=["EDI_SHIP_TO_ESCALATE"],
        )
    if sub_type == "QTY_MISMATCH":
        return ShadowDecisionSchema(
            status="YELLOW",
            reasons=["Quantity mismatch — buyer confirmation required."],
            policy_hits=["EDI_QTY_MISMATCH_REVIEW"],
        )
    if sub_type == "UOM_MISMATCH":
        return ShadowDecisionSchema(
            status="YELLOW",
            reasons=["Unit-of-measure mismatch — buyer confirmation required."],
            policy_hits=["EDI_UOM_MISMATCH_REVIEW"],
        )
    # Unknown sub_type (shouldn't reach here — classifier routes PRICE_MISMATCH
    # elsewhere and unrecognised values fail at recipe input validation).
    return ShadowDecisionSchema(
        status="RED",
        reasons=[f"Unrecognised EDI mismatch sub_type {sub_type!r}."],
        policy_hits=["EDI_UNKNOWN_SUB_TYPE"],
    )


def _shadow_for_back_order(state: GraphState) -> ShadowDecisionSchema:
    """Shadow verdict for BACK_ORDER based on gap_pct vs SEVERE threshold.

    Inputs come from metadata: ordered_qty, available_qty. The recipe
    computes the same gap at execution; shadow pre-gates the call.
    """
    meta = state.event.metadata
    ordered = float(meta.get("ordered_qty") or 0.0)
    available = float(meta.get("available_qty") or 0.0)
    if ordered <= 0:
        return ShadowDecisionSchema(
            status="YELLOW",
            reasons=["Back-order event missing ordered_qty; review required."],
            policy_hits=["BACK_ORDER_MISSING_INPUT"],
        )
    gap_pct = max(0.0, (ordered - available) / ordered)
    if gap_pct >= BACK_ORDER_SEVERE_GAP_PCT:
        return ShadowDecisionSchema(
            status="RED",
            reasons=[
                f"Gap {gap_pct:.2%} at/above severe threshold "
                f"{BACK_ORDER_SEVERE_GAP_PCT:.2%}."
            ],
            policy_hits=["BACK_ORDER_SEVERE_GAP"],
        )
    return ShadowDecisionSchema(
        status="YELLOW",
        reasons=[
            f"Gap {gap_pct:.2%} below severe threshold — resolution options "
            f"presented for review."
        ],
        policy_hits=["BACK_ORDER_MINOR_GAP_REVIEW"],
    )


def _shadow_for_over_max(state: GraphState) -> ShadowDecisionSchema:
    """Shadow verdict for OVER_MAX based on exceedance vs SEVERE threshold."""
    meta = state.event.metadata
    total_ordered = float(meta.get("total_ordered") or 0.0)
    max_qty = float(meta.get("max_qty") or 0.0)
    if max_qty <= 0:
        return ShadowDecisionSchema(
            status="YELLOW",
            reasons=["Over-max event missing max_qty; review required."],
            policy_hits=["OVER_MAX_MISSING_INPUT"],
        )
    exceedance = max(0.0, (total_ordered - max_qty) / max_qty)
    if exceedance > OVER_MAX_SEVERE_EXCEEDANCE_PCT:
        return ShadowDecisionSchema(
            status="RED",
            reasons=[
                f"Exceedance {exceedance:.2%} above severe threshold "
                f"{OVER_MAX_SEVERE_EXCEEDANCE_PCT:.2%}."
            ],
            policy_hits=["OVER_MAX_SEVERE_EXCEEDANCE"],
        )
    return ShadowDecisionSchema(
        status="YELLOW",
        reasons=[
            f"Exceedance {exceedance:.2%} within auto-trim band; operator "
            f"review required before applying trim plan."
        ],
        policy_hits=["OVER_MAX_MINOR_REVIEW"],
    )


def _shadow_for_moq(state: GraphState) -> ShadowDecisionSchema:
    """Shadow verdict for MIN_ORDER_QTY based on shortfall_pct."""
    meta = state.event.metadata
    ordered = float(meta.get("ordered_qty") or 0.0)
    moq = float(meta.get("moq_qty") or 0.0)
    if moq <= 0:
        return ShadowDecisionSchema(
            status="YELLOW",
            reasons=["MOQ event missing moq_qty; review required."],
            policy_hits=["MOQ_MISSING_INPUT"],
        )
    if ordered >= moq:
        return ShadowDecisionSchema(
            status="GREEN",
            reasons=["Ordered qty at/above MOQ — no shortfall."],
            policy_hits=["MOQ_NO_SHORTFALL"],
        )
    shortfall_pct = (moq - ordered) / moq
    if shortfall_pct >= MOQ_SEVERE_SHORTFALL_PCT:
        return ShadowDecisionSchema(
            status="RED",
            reasons=[
                f"Shortfall {shortfall_pct:.2%} at/above severe threshold "
                f"{MOQ_SEVERE_SHORTFALL_PCT:.2%}; KNMT waiver required."
            ],
            policy_hits=["MOQ_SEVERE_SHORTFALL"],
        )
    return ShadowDecisionSchema(
        status="YELLOW",
        reasons=[
            f"Shortfall {shortfall_pct:.2%} within round-up band; "
            f"operator confirmation required."
        ],
        policy_hits=["MOQ_ROUND_UP_REVIEW"],
    )


def _shadow_for_pallet_config(state: GraphState) -> ShadowDecisionSchema:
    """Shadow verdict for PALLET_CONFIG; always YELLOW when violation exists.

    Pallet-config violations don't hard-block orders — they suggest
    round-down or accept-partial paths that must be confirmed by an
    operator. No RED branch unless the pallet-line data is missing.
    """
    lines = state.event.metadata.get("pallet_lines") or []
    if not lines:
        return ShadowDecisionSchema(
            status="YELLOW",
            reasons=["Pallet-config event missing pallet_lines; review required."],
            policy_hits=["PALLET_CONFIG_MISSING_INPUT"],
        )
    return ShadowDecisionSchema(
        status="YELLOW",
        reasons=[
            f"Pallet alignment review required; min-fill threshold "
            f"{PALLET_CONFIG_MIN_FILL_PCT:.0%}."
        ],
        policy_hits=["PALLET_CONFIG_REVIEW"],
    )


def _shadow_for_delivery_delay(state: GraphState) -> ShadowDecisionSchema:
    """Shadow verdict for DELIVERY_DELAY keyed on days_late."""
    meta = state.event.metadata
    days_late = meta.get("days_late")
    if days_late is None:
        # Fall through to the recipe's date computation; treat as YELLOW
        # until we know severity.
        return ShadowDecisionSchema(
            status="YELLOW",
            reasons=["Delivery-delay event pending severity computation."],
            policy_hits=["DELIVERY_DELAY_PENDING_COMPUTE"],
        )
    days_late = int(days_late)
    if days_late >= DELIVERY_DELAY_SEVERE_DAYS:
        return ShadowDecisionSchema(
            status="RED",
            reasons=[
                f"{days_late} days late at/above severe threshold "
                f"{DELIVERY_DELAY_SEVERE_DAYS}."
            ],
            policy_hits=["DELIVERY_DELAY_SEVERE"],
        )
    if days_late >= DELIVERY_DELAY_MINOR_DAYS:
        return ShadowDecisionSchema(
            status="YELLOW",
            reasons=[
                f"{days_late} days late — alternate delivery options "
                f"presented for review."
            ],
            policy_hits=["DELIVERY_DELAY_MINOR_REVIEW"],
        )
    return ShadowDecisionSchema(
        status="GREEN",
        reasons=["Delivery within planned window; no action required."],
        policy_hits=["DELIVERY_DELAY_ON_TIME"],
    )
