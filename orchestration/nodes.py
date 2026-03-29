from __future__ import annotations

# Phase 4 — LangGraph node implementations
#
# Each node does exactly one job:
#   - reads current GraphState
#   - returns updated GraphState (partial update via returned object)
#
# Node rules (CLAUDE.md):
#   - no hidden side effects
#   - no unrelated responsibilities combined
#   - failures are explicit and set final_status + explanation
#   - no MCP / SAP remote calls (production infrastructure concern)
#   - no live LLM calls when backend is DeterministicFallbackBackend
#
# Constrained-generation boundaries (CLAUDE.md §3):
#   - intent constrained to AllowedIntent via backend.classify_intent()
#   - recipe name constrained to AllowedRecipeName via backend.propose_recipe()
#   - shadow verdict constrained to GREEN|YELLOW|RED via backend.shadow_decision()

from contracts.models import (
    GatewayRequest,
    GraphState,
    Intent,
    RecipeInvocation,
    ShadowStatus,
    TerminalStatus,
)
from skills.loader import SkillLoader
from compliance.shadow import ComplianceShadow
from constraints import get_constrained_backend
from constraints.specs import RecipeProposal
from recipes.executor import RecipeExecutor
from recipes.registry import get_recipe
from gateways.executor import GatewayExecutor
from orchestration.utils import circuit_breaker, compute_discrepancy
from hardening.explain_mode import build_explain_summary
from contracts.policy import (
    CREDIT_AUTHORIZED_ROLES,
    CREDIT_EXPOSURE_TOLERANCE,
    DUPLICATE_PO_THRESHOLD_AUTO_BLOCK,
    DUPLICATE_PO_THRESHOLD_REVIEW_REQUIRED,
    DUPLICATE_PO_THRESHOLD_SOFT_FLAG,
    MAX_DISCOUNT_ALLOWED,
    PRICE_CONDITION_TYPE,
)


import logging

_node_logger = logging.getLogger("asoe.nodes")


def _backend():
    """Return the env-driven constrained backend (shared across all nodes)."""
    return get_constrained_backend()


class NodeValidationError(Exception):
    """Raised when a node receives invalid state at a boundary."""


def _validate_event(state: GraphState) -> None:
    """Validate that the inbound event has all required fields for processing.

    Raises NodeValidationError with a structured message instead of
    allowing downstream AttributeError (SEC-2 fix).
    """
    event = state.event
    missing = []
    if not event.order_id:
        missing.append("order_id")
    if event.po_price is None:
        missing.append("po_price")
    if event.sap_base_price is None:
        missing.append("sap_base_price")
    if missing:
        raise NodeValidationError(f"OrderEvent missing required fields: {missing}")


# ---------------------------------------------------------------------------
# ingest — track update count and batch variance
# ---------------------------------------------------------------------------

def ingest(state: GraphState) -> GraphState:
    try:
        _validate_event(state)
    except NodeValidationError as exc:
        _node_logger.error("ingest_validation_failed: %s", exc)
        state.final_status = TerminalStatus.FAIL_TO_HUMAN
        state.explanation = f"Input validation failed: {exc}"
        return state

    state.update_count += 1
    state.batch_total_variance = abs(
        (state.event.po_price - state.event.sap_base_price) * state.event.line_count
    )
    return state


# ---------------------------------------------------------------------------
# classify — constrained intent classification
# ---------------------------------------------------------------------------

def classify(state: GraphState) -> GraphState:
    state.discrepancy = compute_discrepancy(state.event.po_price, state.event.sap_base_price)
    backend = _backend()
    if hasattr(backend, "intent_prompt"):
        # Outlines path: backend expects a prompt string
        decision = backend.classify_intent(backend.intent_prompt(state))
    else:
        # Fallback path: backend accepts full GraphState
        decision = backend.classify_intent(state)
    state.intent = Intent(decision.intent)
    state.confidence = decision.confidence
    return state


# ---------------------------------------------------------------------------
# load_skill — load relevant SKILL.md verbatim
# ---------------------------------------------------------------------------

def load_skill(state: GraphState) -> GraphState:
    loader = SkillLoader("skills")
    state.skill = loader.select_for_event(state.event.event_type)
    return state


# ---------------------------------------------------------------------------
# validate_circuit_breaker — breach → FAIL_TO_HUMAN (tasks.md §4.2)
# ---------------------------------------------------------------------------

def validate_circuit_breaker(state: GraphState) -> GraphState:
    decision = circuit_breaker(
        update_count=state.update_count,
        batch_total_variance=state.batch_total_variance,
    )
    if not decision.allowed:
        state.final_status = TerminalStatus.FAIL_TO_HUMAN
        state.explanation = "; ".join(decision.reasons)
    return state


# ---------------------------------------------------------------------------
# shadow_audit — Compliance Shadow must run before any recipe execution
# ---------------------------------------------------------------------------

def shadow_audit(state: GraphState) -> GraphState:
    # Inject the same constrained backend used by classify/select_recipe
    # so the entire graph uses a consistent generation backend.
    shadow = ComplianceShadow(backend=_backend())
    state.shadow = shadow.audit(state)

    # Use the formal enforcement contract from Phase 2.
    enforcement = shadow.enforce(state.shadow)
    if enforcement.action == "BLOCK":
        state.final_status = TerminalStatus.BLOCKED
        state.explanation = enforcement.explanation
    elif enforcement.action == "ESCALATE":
        state.final_status = TerminalStatus.MANUAL_REVIEW_REQUIRED
        state.explanation = enforcement.explanation
    # PROCEED: leave final_status unset so routing continues
    return state


# ---------------------------------------------------------------------------
# select_recipe — constrained recipe proposal
# ---------------------------------------------------------------------------

def select_recipe(state: GraphState) -> GraphState:
    backend = _backend()
    if hasattr(backend, "recipe_prompt"):
        # Outlines path
        proposal = backend.propose_recipe(backend.recipe_prompt(state))
    else:
        # Fallback path
        proposal = backend.propose_recipe(state)

    if proposal is None:
        state.final_status = TerminalStatus.FAIL_TO_HUMAN
        state.explanation = "No deterministic recipe available for this intent."
        return state

    state.selected_recipe = proposal.recipe_name
    return state


# ---------------------------------------------------------------------------
# validate_types — build typed RecipeInvocation from event fields
# ---------------------------------------------------------------------------

def validate_types(state: GraphState) -> GraphState:
    if state.selected_recipe == "PriceAdjustmentRecipe.py":
        state.invocation = RecipeInvocation(
            recipe_name=state.selected_recipe,
            params={
                "order_id": state.event.order_id,
                "line_item": state.event.line_item,
                "requested_price": state.event.po_price,
                "erp_context": {
                    "base_price": state.event.sap_base_price,
                    "max_discount_allowed": MAX_DISCOUNT_ALLOWED,
                    "condition_type": PRICE_CONDITION_TYPE,
                },
            },
        )
    elif state.selected_recipe == "CreditHoldReleaseRecipe.py":
        state.invocation = RecipeInvocation(
            recipe_name=state.selected_recipe,
            params={
                "order_id": state.event.order_id,
                "requester_role": state.event.requester_role,
                "credit_limit": state.event.credit_limit,
                "current_exposure": state.event.current_exposure,
                "authorized_roles": CREDIT_AUTHORIZED_ROLES,
                "exposure_tolerance": CREDIT_EXPOSURE_TOLERANCE,
            },
        )
    elif state.selected_recipe == "DuplicatePORecipe.py":
        # Resolution context — resolved by gateway dependencies (Phase B).
        # Falls back to None when gateways have not been called (e.g. tests
        # without gateway setup).
        fulfillment = state.resolved_data.get("fulfillment_status", {})
        matched_details = state.resolved_data.get("matched_po_details", {})
        state.invocation = RecipeInvocation(
            recipe_name=state.selected_recipe,
            params={
                "incoming_po_number": state.event.order_id,
                "customer_id": state.event.retailer_id or "",
                "signal_scores": state.event.metadata.get("signal_scores", {}),
                "threshold_auto_block": DUPLICATE_PO_THRESHOLD_AUTO_BLOCK,
                "threshold_review_required": DUPLICATE_PO_THRESHOLD_REVIEW_REQUIRED,
                "threshold_soft_flag": DUPLICATE_PO_THRESHOLD_SOFT_FLAG,
                "original_fulfilled": fulfillment.get("fulfilled", None),
                "has_revision_indicator": matched_details.get("has_revision_indicator", None),
                "line_items_identical": matched_details.get("line_items_identical", None),
            },
        )
    return state


# ---------------------------------------------------------------------------
# explain_only — Phase 6 explain mode: dry-run without execution
# ---------------------------------------------------------------------------

def explain_only(state: GraphState) -> GraphState:
    """Read-only dry-run node that replaces execute_recipe in explain mode.

    Composes a human-readable summary of what WOULD have been executed and
    sets final_status=MANUAL_REVIEW_REQUIRED.  No recipe logic runs, no
    SAP/ERP writes, no MCP calls.

    The Compliance Shadow and circuit breaker both run before this node, so
    the explanation includes the real shadow verdict.
    """
    intent_val = state.intent.value if state.intent else None
    shadow_verdict = None
    policy_hits: list = []
    if state.shadow is not None:
        shadow_verdict = state.shadow.status.value
        policy_hits = list(state.shadow.policy_hits or [])

    invocation_params = None
    if state.invocation is not None:
        invocation_params = state.invocation.params

    state.explanation = build_explain_summary(
        intent=intent_val,
        shadow_verdict=shadow_verdict,
        shadow_policy_hits=policy_hits,
        selected_recipe=state.selected_recipe,
        invocation_params=invocation_params,
    )
    state.final_status = TerminalStatus.MANUAL_REVIEW_REQUIRED
    return state


# ---------------------------------------------------------------------------
# resolve_dependencies — fetch data from gateways before recipe execution
# ---------------------------------------------------------------------------


def resolve_dependencies(state: GraphState) -> GraphState:
    """Call gateway dependencies declared by the selected recipe.

    For each GatewayDependency on the RecipeSpec:
      1. Build GatewayRequest with params resolved from state
      2. Call the gateway via GatewayExecutor
      3. Store result in state.resolved_data[dep.result_key]

    If any dependency fails, halt with FAIL_TO_HUMAN.
    If the recipe has no dependencies, this is a no-op.
    """
    if state.selected_recipe is None:
        return state

    spec = get_recipe(state.selected_recipe)
    if not spec.dependencies:
        return state

    executor = GatewayExecutor()
    trace_id = state.shadow.trace_id if state.shadow else ""

    for dep in spec.dependencies:
        params = {}
        for gw_param, state_path in dep.params_from_state.items():
            value: object = state
            for part in state_path.split("."):
                value = getattr(value, part, None)
                if value is None:
                    break
            params[gw_param] = value

        request = GatewayRequest(
            gateway_name=dep.gateway_name,
            operation=dep.operation,
            params=params,
            trace_id=trace_id,
        )
        response = executor.run(request)

        if response.status != "SUCCESS":
            state.final_status = TerminalStatus.FAIL_TO_HUMAN
            state.explanation = (
                f"Gateway dependency failed: {dep.gateway_name}/{dep.operation}"
                f" — {response.error or response.status}"
            )
            return state

        state.resolved_data[dep.result_key] = response.data

    return state


# ---------------------------------------------------------------------------
# execute_recipe — delegate to RecipeExecutor (Phase 3)
# ---------------------------------------------------------------------------

def execute_recipe(state: GraphState) -> GraphState:
    # Explicit guard — assert is disabled in optimized Python (-O flag).
    if state.invocation is None:
        state.final_status = TerminalStatus.FAIL_TO_HUMAN
        state.explanation = "No recipe invocation available; validate_types may have found no recipe."
        return state

    # Delegate execution to RecipeExecutor (Phase 3).
    # recipe_name in state.invocation is already constrained to AllowedRecipeName
    # via select_recipe → propose_recipe → RecipeProposal.
    proposal = RecipeProposal(recipe_name=state.invocation.recipe_name)  # type: ignore[arg-type]
    trace_id = state.shadow.trace_id if state.shadow else None

    log = RecipeExecutor().run(
        proposal=proposal,
        params=state.invocation.params,
        trace_id=trace_id,
        intent_selected=state.intent.value,
        shadow_policy_hits=state.shadow.policy_hits if state.shadow else [],
    )

    # Attach constrained-output schema names for audit traceability.
    log.constrained_outputs.update({
        "intent": "IntentDecision",
        "shadow": "ShadowDecisionSchema",
        "recipe": "RecipeProposal",
    })
    log.rag_chunks = state.rag_context.chunks
    log.skill_name = state.skill.name if state.skill is not None else None
    log.shadow_verdict = state.shadow.status.value if state.shadow is not None else None
    state.execution_log = log

    # Route terminal status from recipe outcome.
    if log.errors:
        state.final_status = TerminalStatus.FAIL_TO_HUMAN
        state.explanation = "; ".join(log.errors)
        return state

    recipe_status = log.outputs.get("status")
    if recipe_status == "FAILED":
        state.final_status = TerminalStatus.FAIL_TO_HUMAN
        state.explanation = log.outputs.get("reason", "Recipe returned FAILED.")
    elif recipe_status == "BLOCKED":
        state.final_status = TerminalStatus.BLOCKED
        state.explanation = log.outputs.get("reason", "Recipe returned BLOCKED.")
    elif recipe_status == "REJECTED":
        state.final_status = TerminalStatus.REJECTED
        state.explanation = log.outputs.get("reason", "Recipe returned REJECTED.")
    else:
        state.final_status = TerminalStatus.COMPLETE
        state.explanation = "Deterministic execution completed successfully."

    return state


# ---------------------------------------------------------------------------
# apply_effects — call gateways for side effects after recipe execution
# ---------------------------------------------------------------------------


def apply_effects(state: GraphState) -> GraphState:
    """Apply gateway side effects declared by the selected recipe.

    For each GatewayEffect on the RecipeSpec:
      1. Build GatewayRequest with params from recipe output
      2. Call the gateway via GatewayExecutor
      3. Append response to state.effect_results

    Effect failures do NOT undo recipe execution — the recipe result
    stands.  Failed effects are logged and the explanation is updated
    so operators know manual follow-up is needed.

    If the recipe has no effects, this is a no-op.
    """
    if state.selected_recipe is None or state.execution_log is None:
        return state

    spec = get_recipe(state.selected_recipe)
    if not spec.effects:
        return state

    executor = GatewayExecutor()
    trace_id = state.shadow.trace_id if state.shadow else ""

    for effect in spec.effects:
        params = {}
        for gw_param, output_field in effect.params_from_output.items():
            params[gw_param] = state.execution_log.outputs.get(output_field)

        request = GatewayRequest(
            gateway_name=effect.gateway_name,
            operation=effect.operation,
            params=params,
            trace_id=trace_id,
        )
        response = executor.run(request)
        state.effect_results.append(response)

        if response.status != "SUCCESS":
            state.explanation = (
                f"Effect partially failed: {effect.gateway_name}/{effect.operation}"
                f" — {response.error or response.status}. "
                f"Recipe completed but side effect requires manual follow-up."
            )

    return state
