from __future__ import annotations

from typing import Optional

from contracts.models import GraphState, Intent
from contracts.policy import (
    CIRCUIT_BREAKER_MAX_VARIANCE,
    MASS_UPDATE_LINE_COUNT_THRESHOLD,
    PRICE_HOLD_HARD_BLOCK_PCT,
    PRICE_HOLD_TOLERANCE_PCT,
)
from constraints.specs import IntentDecision, RecipeProposal, ShadowDecisionSchema


class DeterministicFallbackBackend:
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
