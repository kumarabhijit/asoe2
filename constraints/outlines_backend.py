from __future__ import annotations

# OutlinesConstrainedBackend — local constrained-generation backend.
#
# Public surface mirrors DeterministicFallbackBackend so callers
# (orchestration/nodes.py, compliance/shadow.py, skills/intent_classifier.py)
# don't need a hasattr/branch on which backend is active. All trio methods
# accept `state: GraphState` and build their prompt internally.
#
# The static prompt builders (intent_prompt / recipe_prompt / shadow_prompt)
# are retained as a stable API for prompt-preview tools (sandbox UI/CLI
# expanders) and tests; they must not be required by graph callers.

from contracts.models import GraphState
from constraints.specs import (
    EmailSupergroupDecision,
    IntentDecision,
    RecipeProposal,
    ShadowDecisionSchema,
)
from llm.backends import get_outlines_model


class OutlinesConstrainedBackend:
    def __init__(self, model_name: str | None = None):
        self.model = get_outlines_model(model_name)

    def classify_email_supergroup(self, state: GraphState) -> EmailSupergroupDecision:
        # ADR-036 Phase 3. The local Outlines path for customer-email
        # classification is not built out (it needs an email-tuned local
        # model); delegate to the deterministic hint-relay so provider=
        # outlines deployments still satisfy the email_supergroup contract
        # rather than raising AttributeError.
        from constraints.fallback_backend import DeterministicFallbackBackend

        return DeterministicFallbackBackend().classify_email_supergroup(state)

    def classify_intent(self, state: GraphState) -> IntentDecision:
        prompt = self.intent_prompt(state)
        raw = self.model(prompt, IntentDecision, max_new_tokens=120)
        return IntentDecision.model_validate_json(raw)

    def propose_recipe(self, state: GraphState) -> RecipeProposal:
        prompt = self.recipe_prompt(state)
        raw = self.model(prompt, RecipeProposal, max_new_tokens=80)
        return RecipeProposal.model_validate_json(raw)

    def shadow_decision(self, state: GraphState) -> ShadowDecisionSchema:
        prompt = self.shadow_prompt(state)
        raw = self.model(prompt, ShadowDecisionSchema, max_new_tokens=120)
        return ShadowDecisionSchema.model_validate_json(raw)

    @staticmethod
    def intent_prompt(state: GraphState) -> str:
        return (
            "Classify the pricing exception into one allowed intent. "
            f"order_id={state.event.order_id}; po_price={state.event.po_price}; "
            f"sap_base_price={state.event.sap_base_price}; line_count={state.event.line_count}; "
            f"requester_role={state.event.requester_role}; credit_limit={state.event.credit_limit}; "
            f"current_exposure={state.event.current_exposure}."
        )

    @staticmethod
    def recipe_prompt(state: GraphState) -> str:
        return (
            "Select the exact registered recipe name for this already-classified intent. "
            f"intent={state.intent.value}."
        )

    @staticmethod
    def shadow_prompt(state: GraphState) -> str:
        return (
            "Return one constrained shadow verdict with reasons and policy hits. "
            f"intent={state.intent.value}; line_count={state.event.line_count}; "
            f"batch_total_variance={state.batch_total_variance}."
        )
