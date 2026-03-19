from __future__ import annotations

from contracts.models import GraphState
from constraints.specs import IntentDecision, RecipeProposal, ShadowDecisionSchema
from llm.backends import get_outlines_model


class OutlinesConstrainedBackend:
    def __init__(self, model_name: str | None = None):
        self.model = get_outlines_model(model_name)

    def classify_intent(self, prompt: str) -> IntentDecision:
        raw = self.model(prompt, IntentDecision, max_new_tokens=120)
        return IntentDecision.model_validate_json(raw)

    def propose_recipe(self, prompt: str) -> RecipeProposal:
        raw = self.model(prompt, RecipeProposal, max_new_tokens=80)
        return RecipeProposal.model_validate_json(raw)

    def shadow_decision(self, prompt: str) -> ShadowDecisionSchema:
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
