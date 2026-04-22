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

import concurrent.futures

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
    BACK_ORDER_SEVERE_GAP_PCT,
    CREDIT_AUTHORIZED_ROLES,
    CREDIT_EXPOSURE_TOLERANCE,
    DELIVERY_DELAY_MINOR_DAYS,
    DELIVERY_DELAY_SEVERE_DAYS,
    DUPLICATE_PO_AUTONOMY_LEVELS,
    DUPLICATE_PO_THRESHOLD_AUTO_BLOCK,
    DUPLICATE_PO_THRESHOLD_REVIEW_REQUIRED,
    DUPLICATE_PO_THRESHOLD_SOFT_FLAG,
    EDI_MISMATCH_AUTONOMY_LEVELS,
    MAX_DISCOUNT_ALLOWED,
    MOQ_SEVERE_SHORTFALL_PCT,
    MOQ_UPLIFT_REVIEW_PCT,
    OVER_MAX_SEVERE_EXCEEDANCE_PCT,
    PALLET_CONFIG_BROKEN_LAYER_FILL_PCT,
    PALLET_CONFIG_MIN_FILL_PCT,
    PRICE_CONDITION_TYPE,
    PRICE_HOLD_HARD_BLOCK_PCT,
    PRICE_HOLD_TOLERANCE_PCT,
)


import logging

_node_logger = logging.getLogger("asoe.nodes")

_cached_backend = None


def _backend():
    """Return the env-driven constrained backend (cached for the process lifetime).

    The backend is stateless — caching the instance avoids redundant env var
    reads and object construction on every node call (3x per graph execution).
    """
    global _cached_backend
    if _cached_backend is None:
        _cached_backend = get_constrained_backend()
    return _cached_backend


def _reset_backend_cache() -> None:
    """Reset the cached backend.  Used by tests that change env vars."""
    global _cached_backend
    _cached_backend = None


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
    state.skill = loader.select_for_event(
        state.event.event_type,
        metadata=state.event.metadata,
    )
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
        fulfillment = state.enrichment_context.get("fulfillment_status", {})
        matched_details = state.enrichment_context.get("matched_po_details", {})
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
                "autonomy_levels": DUPLICATE_PO_AUTONOMY_LEVELS,
            },
        )
    elif state.selected_recipe == "PriceHoldReleaseRecipe.py":
        # Defensive input validation — close the review-flagged
        # ZeroDivisionError path before invocation. The recipe
        # computes variance as (po - sap) / sap, so a zero or
        # negative sap_base_price would raise at execution time.
        # Catch it here and route to FAIL_TO_HUMAN with a named
        # reason; don't rely on a generic executor-level catch.
        if state.event.sap_base_price <= 0:
            state.final_status = TerminalStatus.FAIL_TO_HUMAN
            state.explanation = (
                f"PriceHoldReleaseRecipe requires sap_base_price > 0; "
                f"got {state.event.sap_base_price!r}. Order cannot be "
                f"classified deterministically; routing to human review."
            )
            return state
        # Optional per-event tolerance override via metadata.tolerance_pct.
        tolerance = state.event.metadata.get(
            "tolerance_pct", PRICE_HOLD_TOLERANCE_PCT
        )
        # Prefer the live gateway result (oms/get_price_hold_status returns
        # {"status": "HELD"}); fall back to metadata when the gateway response
        # lacks that key.
        gateway_result = state.enrichment_context.get("price_hold_status", {})
        hold_status = (
            gateway_result.get("status")
            if isinstance(gateway_result, dict) else None
        ) or state.event.metadata.get("price_hold_status", "HELD")
        state.invocation = RecipeInvocation(
            recipe_name=state.selected_recipe,
            params={
                "order_id": state.event.order_id,
                "line_item": state.event.line_item,
                "po_price": state.event.po_price,
                "sap_base_price": state.event.sap_base_price,
                "tolerance_pct": tolerance,
                "hard_block_pct": PRICE_HOLD_HARD_BLOCK_PCT,
                "hold_status": hold_status,
                "requester_role": state.event.requester_role,
            },
        )
    elif state.selected_recipe == "EdiMismatchRecipe.py":
        state.invocation = RecipeInvocation(
            recipe_name=state.selected_recipe,
            params={
                "order_id": state.event.order_id,
                "sub_type": state.event.metadata.get("mismatch_sub_type"),
                "expected_value": state.event.metadata.get("expected_value"),
                "received_value": state.event.metadata.get("received_value"),
                "autonomy_levels": EDI_MISMATCH_AUTONOMY_LEVELS,
            },
        )
    elif state.selected_recipe == "BackOrderResolutionRecipe.py":
        meta = state.event.metadata
        state.invocation = RecipeInvocation(
            recipe_name=state.selected_recipe,
            params={
                "order_id": state.event.order_id,
                "sku": state.event.sku or meta.get("sku") or "",
                "ordered_qty": float(meta.get("ordered_qty") or 0.0),
                "available_qty": float(meta.get("available_qty") or 0.0),
                "unit_price": float(meta.get("unit_price") or 0.0),
                "uom": meta.get("uom") or "CS",
                "severe_gap_pct": BACK_ORDER_SEVERE_GAP_PCT,
                "alternate_warehouses": meta.get("alternate_warehouses"),
                "substitutes": meta.get("substitutes"),
            },
        )
    elif state.selected_recipe == "OverMaxTrimRecipe.py":
        meta = state.event.metadata
        state.invocation = RecipeInvocation(
            recipe_name=state.selected_recipe,
            params={
                "order_id": state.event.order_id,
                "total_ordered": float(meta.get("total_ordered") or 0.0),
                "max_qty": float(meta.get("max_qty") or 0.0),
                "severe_exceedance_pct": OVER_MAX_SEVERE_EXCEEDANCE_PCT,
                "order_lines": meta.get("order_lines"),
                "unit_cost_per_line": meta.get("unit_cost_per_line"),
            },
        )
    elif state.selected_recipe == "MOQRoundUpRecipe.py":
        meta = state.event.metadata
        state.invocation = RecipeInvocation(
            recipe_name=state.selected_recipe,
            params={
                "order_id": state.event.order_id,
                "sku": state.event.sku or meta.get("sku") or "",
                "ordered_qty": float(meta.get("ordered_qty") or 0.0),
                "moq_qty": float(meta.get("moq_qty") or 0.0),
                "unit_cost": float(meta.get("unit_cost") or 0.0),
                "uom": meta.get("uom") or "CS",
                "severe_shortfall_pct": MOQ_SEVERE_SHORTFALL_PCT,
                "uplift_review_pct": MOQ_UPLIFT_REVIEW_PCT,
            },
        )
    elif state.selected_recipe == "PalletAlignmentRecipe.py":
        meta = state.event.metadata
        state.invocation = RecipeInvocation(
            recipe_name=state.selected_recipe,
            params={
                "order_id": state.event.order_id,
                "lines": meta.get("pallet_lines"),
                "min_fill_pct": PALLET_CONFIG_MIN_FILL_PCT,
                "broken_layer_fill_pct": PALLET_CONFIG_BROKEN_LAYER_FILL_PCT,
            },
        )
    elif state.selected_recipe == "DeliveryDelayResolutionRecipe.py":
        meta = state.event.metadata
        state.invocation = RecipeInvocation(
            recipe_name=state.selected_recipe,
            params={
                "order_id": state.event.order_id,
                "planned_date": meta.get("planned_date") or "",
                "projected_eta": meta.get("projected_eta") or "",
                "minor_days": DELIVERY_DELAY_MINOR_DAYS,
                "severe_days": DELIVERY_DELAY_SEVERE_DAYS,
                "carrier": meta.get("carrier"),
                "route": meta.get("route"),
                "delay_category": meta.get("delay_category"),
                "alternate_options": meta.get("alternate_options"),
            },
        )
    elif state.selected_recipe is not None:
        # Explicit failure — a known recipe name with no validate_types branch
        # is a routing bug, not a business exception. Route FAIL_TO_HUMAN with
        # the offending recipe name instead of silently producing
        # state.invocation=None and letting execute_recipe emit a generic error.
        # selected_recipe=None is already handled upstream by select_recipe.
        state.final_status = TerminalStatus.FAIL_TO_HUMAN
        state.explanation = (
            f"validate_types has no branch for selected_recipe="
            f"{state.selected_recipe!r}; refusing to execute an unvalidated "
            f"invocation."
        )
    return state


# ---------------------------------------------------------------------------
# explain_only — Phase 6 explain mode: dry-run without execution
# ---------------------------------------------------------------------------

def explain_only(state: GraphState) -> GraphState:
    """Read-only dry-run node that replaces execute_recipe in explain mode.

    Composes a human-readable summary of what WOULD have been executed and
    sets final_status=MANUAL_REVIEW_REQUIRED.  No recipe logic runs, no
    SAP/ERP writes, no MCP calls. Gateway READS (resolve_dependencies)
    do run earlier in the explain graph so the audit context the
    explanation references matches the live path.

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
      3. Store result in state.enrichment_context[dep.result_key]

    Verdict Pillar 1: gateway-fetched evidence lives in enrichment_context
    (audit-bearing, persisted to ExceptionRecord.enrichment_context).
    Recipe-input sites read from the same bag — one source of truth.

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

    # Build all requests up front so dependencies can be resolved concurrently.
    requests = []
    for dep in spec.dependencies:
        params = {}
        for gw_param, state_path in dep.params_from_state.items():
            value: object = state
            for part in state_path.split("."):
                value = getattr(value, part, None)
                if value is None:
                    break
            params[gw_param] = value

        requests.append((dep, GatewayRequest(
            gateway_name=dep.gateway_name,
            operation=dep.operation,
            params=params,
            trace_id=trace_id,
        )))

    # Resolve independent dependencies concurrently.
    future_to_dep = {
        executor._pool.submit(executor.run, req): dep
        for dep, req in requests
    }
    for future in concurrent.futures.as_completed(future_to_dep):
        dep = future_to_dep[future]
        response = future.result()

        if response.status != "SUCCESS":
            state.final_status = TerminalStatus.FAIL_TO_HUMAN
            state.explanation = (
                f"Gateway dependency failed: {dep.gateway_name}/{dep.operation}"
                f" — {response.error or response.status}"
            )
            return state

        state.enrichment_context[dep.result_key] = response.data

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

    # Autonomy routing takes precedence when present: L1/L2 actions require
    # human approval regardless of recipe classification status.
    autonomy = log.outputs.get("autonomy_level")
    if autonomy in ("L1", "L2"):
        state.final_status = TerminalStatus.MANUAL_REVIEW_REQUIRED
        action = log.outputs.get("recommended_action", "")
        state.explanation = (
            f"Autonomy level {autonomy}: {action} requires human approval."
        )
    elif recipe_status == "FAILED":
        state.final_status = TerminalStatus.FAIL_TO_HUMAN
        state.explanation = log.outputs.get("reason", "Recipe returned FAILED.")
    elif recipe_status == "BLOCKED":
        state.final_status = TerminalStatus.BLOCKED
        state.explanation = log.outputs.get("reason", "Recipe returned BLOCKED.")
    elif recipe_status == "REJECTED":
        state.final_status = TerminalStatus.REJECTED
        state.explanation = log.outputs.get("reason", "Recipe returned REJECTED.")
    elif recipe_status == "REVIEW_REQUIRED":
        state.final_status = TerminalStatus.MANUAL_REVIEW_REQUIRED
        state.explanation = log.outputs.get("reason", "Recipe returned REVIEW_REQUIRED.")
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


# ---------------------------------------------------------------------------
# build_analysis — Verdict Pillar 2: registry-enforced CQRS read model.
#
# Runs at the TAIL of every graph path (GREEN success, YELLOW manual
# review, RED block, and every terminal routing in between) so the
# audit-bearing field registry at
# `compliance/audit_bearing_registry.yaml` is enforced before
# persistence. Two outcomes:
#
#   * Coverage complete → state passes through unchanged. Downstream
#     /analysis endpoint reads the composer's projection on demand.
#   * Coverage incomplete → final_status is upgraded to
#     AUDIT_CONTEXT_MISSING (distinct from FAIL_TO_HUMAN so auditors
#     see "compliance data was missing" rather than "pipeline
#     crashed"). The explanation names the missing fields so the
#     trace is actionable.
#
# This node NEVER mutates resolution_data, execution_log.outputs, or
# shadow decisions — enforcing the registry is a compliance gate,
# not a business-logic override (CLAUDE.md §1).
# ---------------------------------------------------------------------------


def build_analysis(state: GraphState) -> GraphState:
    # Defensive re-entry guard: if a prior node already set
    # AUDIT_CONTEXT_MISSING, leave it alone. Don't double-append
    # missing-field lists.
    if state.final_status == TerminalStatus.AUDIT_CONTEXT_MISSING:
        return state

    # Lazy import — keeps orchestration independent of the API layer
    # in the import graph so test isolation stays clean.
    from api.analysis_composer import compose_from_state

    composed = compose_from_state(state)

    if not composed.should_route_to_audit_context_missing:
        return state

    missing_list = ", ".join(composed.missing_audit_fields)
    preamble = state.explanation or ""
    suffix = (
        f"Audit-bearing fields missing from "
        f"{composed.class_name}: [{missing_list}]. Record cannot be "
        f"presented to an operator without authoritative values for "
        f"these fields — see "
        f"compliance/audit_bearing_registry.yaml."
    )
    state.final_status = TerminalStatus.AUDIT_CONTEXT_MISSING
    state.explanation = f"{preamble}\n\n{suffix}" if preamble else suffix
    return state
