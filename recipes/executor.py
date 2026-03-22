from __future__ import annotations

# Phase 3 — Recipe Invocation
#
# RecipeExecutor is the sole entry point for deterministic recipe execution.
#
# Responsibilities:
#   run() — accept a constrained RecipeProposal + validated params, look up
#            the recipe in the registry, validate required params, call the
#            recipe function, return an immutable ExecutionLog.
#
# Invariants:
#   - RecipeProposal.recipe_name is constrained at generation time to
#     AllowedRecipeName via Guidance / Outlines (constraints/specs.py).
#     The executor treats this as the primary gate; registry lookup is a
#     defence-in-depth check.
#   - NO ComplianceShadow call here (Phase 2 concern).
#   - NO LangGraph / graph state mutation (Phase 4 concern).
#   - NO MCP / SAP remote calls (production infrastructure concern).
#   - Recipe functions (PriceAdjustmentRecipe, CreditHoldReleaseRecipe) are
#     called as-is; their logic is never modified or reimplemented here.
#   - ExecutionLog is returned, never mutated after construction.

from typing import Any, Dict, List, Optional
from uuid import uuid4

from contracts.models import ExecutionLog
from constraints.specs import RecipeProposal
from recipes.registry import REGISTRY, get_recipe


class RecipeExecutor:
    """Deterministic execution wrapper for registered recipes.

    Usage::

        proposal = RecipeProposal(recipe_name="PriceAdjustmentRecipe.py")
        log = RecipeExecutor().run(
            proposal=proposal,
            params={
                "order_id": "SO-1001",
                "line_item": 1,
                "requested_price": 92.0,
                "erp_context": {
                    "base_price": 100.0,
                    "max_discount_allowed": 0.15,
                    "condition_type": "YK07",
                },
            },
            trace_id="<uuid from ComplianceDecision>",
            intent_selected="CONTRACTUAL_CORRECTION",
        )
    """

    # ------------------------------------------------------------------
    # run — Phase 3.2 deterministic execution wrapper
    # ------------------------------------------------------------------

    def run(
        self,
        proposal: RecipeProposal,
        params: Dict[str, Any],
        trace_id: Optional[str] = None,
        intent_selected: Optional[str] = None,
        shadow_policy_hits: Optional[List[str]] = None,
    ) -> ExecutionLog:
        """Execute the recipe named in *proposal* and return an immutable log.

        Args:
            proposal:          Constrained RecipeProposal (recipe_name is an
                               AllowedRecipeName — validated by Pydantic at
                               construction time via Guidance/Outlines).
            params:            Parameter dict to pass to the recipe function.
            trace_id:          TraceID from the upstream ComplianceDecision.
                               Defaults to a new UUID when called standalone.
            intent_selected:   Intent value for audit traceability.
            shadow_policy_hits: Policy hits from ComplianceDecision for audit.

        Returns:
            ExecutionLog — immutable record of inputs, outputs, and any errors.

        Raises:
            KeyError: if recipe_name is not in the registry (defence-in-depth;
                      normally prevented by the AllowedRecipeName constraint).
        """
        effective_trace_id = trace_id or str(uuid4())
        recipe_name = proposal.recipe_name

        # Defence-in-depth registry lookup — raises KeyError for unknown names.
        spec = get_recipe(recipe_name)

        # Validate all required params are present and non-None.
        missing = [
            k for k in spec.required_params
            if k not in params or params[k] is None
        ]
        if missing:
            return ExecutionLog(
                trace_id=effective_trace_id,
                recipe_name=recipe_name,
                inputs=params,
                outputs={},
                errors=[f"Missing required parameters: {missing}"],
                constrained_outputs={"recipe": "RecipeProposal"},
                intent_selected=intent_selected,
                shadow_policy_hits=shadow_policy_hits or [],
            )

        # Call the immutable recipe function — logic is never reimplemented here.
        try:
            outputs = spec.func(**params)
        except Exception as exc:
            return ExecutionLog(
                trace_id=effective_trace_id,
                recipe_name=recipe_name,
                inputs=params,
                outputs={},
                errors=[f"Recipe raised {type(exc).__name__}: {exc}"],
                constrained_outputs={"recipe": "RecipeProposal"},
                intent_selected=intent_selected,
                shadow_policy_hits=shadow_policy_hits or [],
            )

        return ExecutionLog(
            trace_id=effective_trace_id,
            recipe_name=recipe_name,
            inputs=params,
            outputs=outputs,
            errors=[],
            constrained_outputs={
                "recipe": "RecipeProposal",        # schema used to constrain name
                "intent": "IntentDecision",         # schema used to constrain intent
                "shadow": "ShadowDecisionSchema",   # schema used to constrain verdict
            },
            intent_selected=intent_selected,
            shadow_policy_hits=shadow_policy_hits or [],
        )

    # ------------------------------------------------------------------
    # registry helpers (read-only, no execution)
    # ------------------------------------------------------------------

    @staticmethod
    def registered_names() -> List[str]:
        """Return all registered recipe names (mirrors AllowedRecipeName)."""
        return list(REGISTRY.keys())

    @staticmethod
    def is_registered(name: str) -> bool:
        """Return True if *name* is in the registry."""
        return name in REGISTRY
