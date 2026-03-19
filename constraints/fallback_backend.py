from __future__ import annotations

from typing import Optional

from contracts.models import GraphState, Intent
from constraints.specs import IntentDecision, RecipeProposal, ShadowDecisionSchema


class DeterministicFallbackBackend:
    def classify_intent(self, state: GraphState) -> IntentDecision:
        if state.event.line_count > 10:
            return IntentDecision(intent="MASS_PRICING_ERROR", confidence=0.95, rationale="line_count > 10")
        if state.event.event_type in ("EDI_850_DUPLICATE_PO", "DUPLICATE"):
            return IntentDecision(intent="DUPLICATE_PO", confidence=0.90, rationale="duplicate PO event type")
        if state.event.requester_role and state.event.credit_limit is not None and state.event.current_exposure is not None:
            return IntentDecision(intent="CREDIT_BLOCK", confidence=0.80, rationale="credit fields present")
        return IntentDecision(intent="CONTRACTUAL_CORRECTION", confidence=0.90, rationale="within pricing path")

    def propose_recipe(self, state: GraphState) -> Optional[RecipeProposal]:
        mapping = {
            Intent.CONTRACTUAL_CORRECTION: "PriceAdjustmentRecipe.py",
            Intent.CREDIT_BLOCK: "CreditHoldReleaseRecipe.py",
            Intent.DUPLICATE_PO: "DuplicatePORecipe.py",
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
        if state.batch_total_variance > 10_000:
            return ShadowDecisionSchema(
                status="RED",
                reasons=["Circuit breaker threshold exceeded for total batch variance."],
                policy_hits=["CIRCUIT_BREAKER_VARIANCE"],
            )
        if state.event.line_count > 10:
            return ShadowDecisionSchema(
                status="RED",
                reasons=[">10 line items suggests mass update risk."],
                policy_hits=["MASS_UPDATE_DETECTED"],
            )
        if state.intent == Intent.CREDIT_BLOCK:
            return ShadowDecisionSchema(
                status="YELLOW",
                reasons=["Credit path requires manual review by policy."],
                policy_hits=["CREDIT_RELEASE_REVIEW"],
            )
        return ShadowDecisionSchema(
            status="GREEN",
            reasons=["No blocking policy hit detected."],
            policy_hits=[],
        )
