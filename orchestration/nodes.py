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
import os
import threading
from datetime import datetime, timezone
from typing import Any

from contracts.models import (
    ExecutedNode,
    GatewayCallSpan,
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
from constraints.cross_check import cross_check
from constraints.fallback_backend import DeterministicFallbackBackend
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
    EMAIL_ORDER_AUTO_APPROVE_CONFIDENCE,
    EMAIL_ORDER_AUTO_CORRECT_CONFIDENCE,
    EMAIL_ORDER_ENTRY_AUTONOMY_LEVELS,
    EMAIL_ORDER_REVIEW_BAND_LOW,
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


# ---------------------------------------------------------------------------
# ADR-027 Phase B — per-node executed-trace recording helper
# ---------------------------------------------------------------------------
#
# Each node calls `_record(state, ...)` once before every `return state`.
# The helper appends an `ExecutedNode` to `state.execution_trace` carrying
# the node's name, timing, decision payload, and the verdict-on-exit
# (when the node exits via a conditional gate or implicit halt).
#
# `entered_at` is captured at the top of each node; `_record` stamps
# `completed_at` and computes `duration_ms`. The status is "halted" when
# the node set `final_status` itself (terminal route) and "completed"
# otherwise. "errored" is reserved for nodes that catch an unexpected
# exception — today only ingest's NodeValidationError flows through this
# path; future error paths can opt in.

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record(
    state: GraphState,
    *,
    node: str,
    entered_at: str,
    decision: dict | None = None,
    exit_verdict: str | None = None,
    policy_hits: list[str] | None = None,
    sub_spans: list[GatewayCallSpan] | None = None,
    status: str | None = None,
) -> None:
    """Append one `ExecutedNode` entry to `state.execution_trace`.

    `status` defaults to "halted" if `state.final_status` is set,
    "completed" otherwise. Pass `status="errored"` explicitly for
    exception-caught paths.
    """
    completed_at = _now_iso()
    try:
        delta = (
            datetime.fromisoformat(completed_at)
            - datetime.fromisoformat(entered_at)
        )
        duration_ms = max(0, int(delta.total_seconds() * 1000))
    except (TypeError, ValueError):
        duration_ms = None

    if status is None:
        status = "halted" if state.final_status is not None else "completed"

    state.execution_trace.append(
        ExecutedNode(
            node=node,
            entered_at=entered_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            timestamp=entered_at,
            status=status,  # type: ignore[arg-type]
            decision=decision or {},
            exit_verdict=exit_verdict,
            policy_hits=policy_hits or [],
            sub_spans=sub_spans or [],
        )
    )

# Per-task backend cache. The router resolves to a different provider
# per task ('intent' / 'recipe' / 'shadow') based on ASOE_LLM_PROVIDER
# + ASOE_LLM_PROVIDER_<TASK> + ASOE_LLM_DISABLE_FOR — see
# constraints/router.py. We cache per task because constructing a
# remote-provider SDK client (Anthropic / OpenAI / Ollama / HF) is
# non-trivial and most graph runs hit each task exactly once.
#
# Tests that flip env vars must call `_reset_backend_cache()` to
# clear stale entries.
_cached_backends: dict[Any, Any] = {}


def _backend(task: Any = None):
    """Return the env-driven constrained backend for the given task.

    Args:
        task: 'intent' | 'recipe' | 'shadow' | None. None preserves
            legacy back-compat (no per-task override applied).

    The backend is cached per task for the process lifetime; the
    underlying clients are stateless.
    """
    if task not in _cached_backends:
        _cached_backends[task] = get_constrained_backend(task=task)
    return _cached_backends[task]


def _reset_backend_cache() -> None:
    """Reset all cached backends. Used by tests that change env vars."""
    _cached_backends.clear()


def _drain_llm_trace(state: GraphState, backend: Any) -> None:
    """If the backend exposes a `last_call_trace` (RemoteLLMBackend
    does; DeterministicFallbackBackend does not), append it to
    `state.llm_call_traces` and clear the slot so a follow-up call
    on the same backend instance doesn't double-record.

    Called from each trio call site (classify / select_recipe /
    shadow_audit) so per-call telemetry survives onto GraphState
    without changing the trio interface.
    """
    trace = getattr(backend, "last_call_trace", None)
    if trace is None:
        return
    state.llm_call_traces.append(trace)
    backend.last_call_trace = None


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
    entered_at = _now_iso()
    try:
        _validate_event(state)
    except NodeValidationError as exc:
        _node_logger.error("ingest_validation_failed: %s", exc)
        state.final_status = TerminalStatus.FAIL_TO_HUMAN
        state.explanation = f"Input validation failed: {exc}"
        _record(
            state, node="ingest", entered_at=entered_at,
            decision={"validation_error": str(exc)},
            status="errored",
        )
        return state

    # Stamp a request-scoped trace ID before any node fans out — used
    # by resolve_dependencies / apply_effects for gateway-call
    # correlation (independent of the ComplianceDecision trace ID
    # produced later by shadow_audit).
    if not state.request_trace_id:
        import uuid as _uuid
        state.request_trace_id = str(_uuid.uuid4())

    state.update_count += 1
    state.batch_total_variance = abs(
        (state.event.po_price - state.event.sap_base_price) * state.event.line_count
    )
    _record(
        state, node="ingest", entered_at=entered_at,
        decision={
            "order_id": state.event.order_id,
            "request_trace_id": state.request_trace_id,
        },
    )
    return state


# ---------------------------------------------------------------------------
# classify — constrained intent classification
# ---------------------------------------------------------------------------

def classify(state: GraphState) -> GraphState:
    entered_at = _now_iso()
    state.discrepancy = compute_discrepancy(state.event.po_price, state.event.sap_base_price)
    backend = _backend(task="intent")
    decision = backend.classify_intent(state)

    # Cross-check: when an LLM-backed classifier is active, run the
    # deterministic classifier in parallel and route to MANUAL_REVIEW_-
    # REQUIRED on disagreement. Conservative shakeout posture per
    # CLAUDE.md §5 (MANUAL_REVIEW_REQUIRED is a valid terminal state)
    # and the user-approved policy.
    #
    # Skip when the active backend is already DeterministicFallbackBackend
    # — comparing the deterministic output to itself can never disagree
    # and just wastes CPU.
    if not isinstance(backend, DeterministicFallbackBackend):
        det_decision = DeterministicFallbackBackend().classify_intent(state)
        check = cross_check(
            llm_decision=decision,
            deterministic_decision=det_decision,
        )
        # Drain LLM call trace BEFORE we apply terminal-state routing
        # so the disagreement signal can be stamped on it. The trace
        # carries the cross-check outcome alongside the provider
        # telemetry — single audit record per call.
        _drain_llm_trace(state, backend)
        if state.llm_call_traces:
            last_trace = state.llm_call_traces[-1]
            # Pydantic frozen=False on LLMCallTrace; we mutate the
            # latest entry in place to set the cross-check signal.
            last_trace.cross_check_disagreement = not check.agreed
            last_trace.cross_check_llm_intent = check.llm_intent
            last_trace.cross_check_deterministic_intent = check.deterministic_intent

        if not check.agreed:
            # Deterministic intent wins for downstream routing; the
            # graph is forced to MANUAL_REVIEW_REQUIRED.
            state.intent = Intent(check.winning_decision.intent)
            state.confidence = check.winning_decision.confidence
            state.final_status = TerminalStatus.MANUAL_REVIEW_REQUIRED
            state.explanation = (
                f"LLM/deterministic classification disagreement: "
                f"llm={check.llm_intent}, "
                f"deterministic={check.deterministic_intent}. "
                f"Routing to manual review per cross-check policy "
                f"({check.reason})."
            )
            _node_logger.warning(
                "classify.cross_check_disagreement",
                extra={
                    "llm_intent": check.llm_intent,
                    "deterministic_intent": check.deterministic_intent,
                    "reason": check.reason,
                    "order_id": state.event.order_id,
                },
            )
            _record(
                state, node="classify", entered_at=entered_at,
                decision={
                    "intent": state.intent.value,
                    "confidence": state.confidence,
                    "llm_intent": check.llm_intent,
                    "deterministic_intent": check.deterministic_intent,
                    "cross_check_reason": check.reason,
                },
                exit_verdict="cross_check_disagreement",
            )
            return state
    else:
        # Deterministic-only path — no LLM trace to drain, but the
        # call itself is still cheap.
        _drain_llm_trace(state, backend)

    state.intent = Intent(decision.intent)
    state.confidence = decision.confidence
    _record(
        state, node="classify", entered_at=entered_at,
        decision={
            "intent": state.intent.value,
            "confidence": state.confidence,
        },
        exit_verdict="ok",
    )
    return state


# ---------------------------------------------------------------------------
# load_skill — load relevant SKILL.md verbatim
# ---------------------------------------------------------------------------

def load_skill(state: GraphState) -> GraphState:
    entered_at = _now_iso()
    loader = SkillLoader("skills")
    state.skill = loader.select_for_event(
        state.event.event_type,
        metadata=state.event.metadata,
    )
    _record(
        state, node="load_skill", entered_at=entered_at,
        decision={"skill_name": state.skill.name if state.skill else None},
    )
    return state


# ---------------------------------------------------------------------------
# validate_circuit_breaker — breach → FAIL_TO_HUMAN (tasks.md §4.2)
# ---------------------------------------------------------------------------

def validate_circuit_breaker(state: GraphState) -> GraphState:
    entered_at = _now_iso()
    decision = circuit_breaker(
        update_count=state.update_count,
        batch_total_variance=state.batch_total_variance,
    )
    if not decision.allowed:
        state.final_status = TerminalStatus.FAIL_TO_HUMAN
        state.explanation = "; ".join(decision.reasons)
        _record(
            state, node="validate_circuit_breaker", entered_at=entered_at,
            decision={
                "update_count": state.update_count,
                "batch_total_variance": state.batch_total_variance,
                "reasons": decision.reasons,
            },
            exit_verdict="breach",
        )
        return state
    _record(
        state, node="validate_circuit_breaker", entered_at=entered_at,
        decision={
            "update_count": state.update_count,
            "batch_total_variance": state.batch_total_variance,
        },
        exit_verdict="ok",
    )
    return state


# ---------------------------------------------------------------------------
# shadow_audit — Compliance Shadow must run before any recipe execution
# ---------------------------------------------------------------------------

# ADR-039 X.1 observe-only L2 LLM Shadow wire-up. Lazily-built singleton
# so test fixtures that monkeypatch env vars on import-time stay
# deterministic. Ops can disable at runtime by setting
# ``ASOE_SHADOW_LLM_DISABLED=1``; the deterministic verdict path is
# unaffected (X.1 is observe-only by definition — even when invoked,
# the verdict does not move state.shadow.status).
_l2_shadow_singleton: Any = None
_l2_shadow_lock = threading.RLock()


def _l2_shadow() -> Any:
    """Lazy-build (and cache) the X.1 ``ShadowLLM`` singleton."""
    global _l2_shadow_singleton
    if _l2_shadow_singleton is not None:
        return _l2_shadow_singleton
    with _l2_shadow_lock:
        if _l2_shadow_singleton is not None:
            return _l2_shadow_singleton
        # Local import keeps the orchestration module importable even
        # if the optional shadow_llm bundle is missing (CI surfaces
        # the bundle as a hard dependency in tests).
        from compliance.shadow_llm import ShadowLLM, StubLLMShadowProvider, load_bundle

        bundle = load_bundle()
        _l2_shadow_singleton = ShadowLLM(
            provider=StubLLMShadowProvider(),
            bundle=bundle,
        )
        return _l2_shadow_singleton


def _reset_l2_shadow_cache() -> None:
    """Test helper — drop the cached singleton so a fresh
    ``StubLLMShadowProvider`` is built on the next call."""
    global _l2_shadow_singleton
    with _l2_shadow_lock:
        _l2_shadow_singleton = None


def _shadow_llm_disabled() -> bool:
    """Honour the runtime kill switch. Truthy values: ``1`` / ``true``
    (case-insensitive). Default: enabled (post-ADR-039 §6.1
    ratification)."""
    raw = os.getenv("ASOE_SHADOW_LLM_DISABLED", "").strip().lower()
    return raw in ("1", "true", "yes")


def _estimate_financial_impact_usd(state: GraphState) -> float:
    """Best-effort impact estimate at shadow-audit time.

    The recipe's ``financial_impact_usd`` is only available after
    ``execute_recipe`` runs (post-shadow). The cleanest pre-recipe
    proxy is the price-gap × line_count already computed at ingest
    (``state.batch_total_variance``). When the gap is zero (non-
    pricing event, e.g. CREDIT_BLOCK with no variance), the L2
    Shadow's GREEN-path floor is not met — but the YELLOW path
    still triggers regardless of impact, which is the load-bearing
    guarantee per ADR-039 §5.2.
    """
    impact = float(state.batch_total_variance or 0.0)
    metadata = state.event.metadata or {}
    explicit = metadata.get("financial_impact_usd")
    if isinstance(explicit, (int, float)):
        impact = max(impact, float(explicit))
    return impact


def _invoke_l2_shadow_observe_only(state: GraphState) -> None:
    """ADR-039 §6.1 X.1 observe-only invocation.

    Stamps ``state.shadow.llm_shadow_verdict`` when the gating
    triggers fire (deterministic-YELLOW OR financial-impact ≥
    bundle floor). NEVER moves ``state.shadow.status`` — that is
    the X.2+ behaviour and lands behind a separate Compliance gate.

    Failure modes (provider down, timeout, validation error) are
    fully absorbed by ``ShadowLLM.evaluate``; this helper logs the
    skip reason and otherwise leaves the deterministic path
    unchanged.
    """
    if state.shadow is None:
        return
    if _shadow_llm_disabled():
        return
    try:
        s = _l2_shadow()
    except FileNotFoundError:
        # Bundle absent (e.g. test environment without knowledge/);
        # silently skip the L2 path so the deterministic verdict is
        # the only thing the caller sees.
        return

    from compliance.shadow_llm import ShadowLLMRequest

    impact = _estimate_financial_impact_usd(state)
    request = ShadowLLMRequest(
        intent=str(state.intent.value if state.intent else ""),
        recipe_name=state.selected_recipe or "",
        recipe_params={"order_id": state.event.order_id},
        proposed_action="",  # recipe hasn't run; we don't know yet
        deterministic_status=state.shadow.status.value,
        deterministic_reasons=tuple(state.shadow.reasons or []),
        deterministic_policy_hits=tuple(state.shadow.policy_hits or []),
        case_context_summary=None,
        customer_profile={
            "tenant_id": state.tenant_id or "",
            "retailer_id": state.event.retailer_id or "",
        },
    )
    outcome = s.evaluate(
        tenant_id=state.tenant_id or "default",
        request=request,
        deterministic=state.shadow,
        financial_impact_usd=impact,
    )
    if outcome.verdict is None:
        return  # Gating skipped or provider failed; SLI counters
                # already record the skip.

    # X.1 invariant: stamp the verdict on the existing
    # ComplianceDecision but do NOT mutate `status`. The harness
    # records it for the audit trail; downstream enforcement
    # continues to use only state.shadow.status.
    state.shadow = state.shadow.model_copy(
        update={"llm_shadow_verdict": outcome.verdict},
    )

    # Append an LLMCallTrace entry tagged task='shadow_llm' so
    # the existing per-call telemetry pipeline picks up the
    # invocation. The provider doesn't expose a real trace today
    # (StubLLMShadowProvider) so we synthesise a minimal one.
    from contracts.models import LLMCallTrace

    state.llm_call_traces.append(
        LLMCallTrace(
            task="shadow_llm",
            provider="stub",
            model_id=outcome.verdict.model_id,
            request_id=outcome.verdict.request_id,
            input_tokens=0,
            output_tokens=0,
            latency_ms=outcome.verdict.latency_ms,
            cost_usd_estimate=outcome.verdict.cost_usd_estimate,
        )
    )


def shadow_audit(state: GraphState) -> GraphState:
    entered_at = _now_iso()
    # Per-task backend selection: shadow defaults to deterministic in
    # V1 PR-1 (compliance integrity) and can be flipped per-tenant
    # via ASOE_LLM_PROVIDER_SHADOW only after compliance sign-off.
    # ASOE_LLM_DISABLE_FOR=shadow forces deterministic regardless of
    # provider config — runtime kill-by-task without a redeploy.
    backend = _backend(task="shadow")
    shadow = ComplianceShadow(backend=backend)
    state.shadow = shadow.audit(state)
    _drain_llm_trace(state, backend)

    # ADR-039 X.1 — observe-only L2 LLM Shadow second opinion. Runs
    # after the deterministic L1 gate has produced its verdict.
    # Stamps `state.shadow.llm_shadow_verdict` for telemetry; never
    # changes the verdict itself in X.1.
    _invoke_l2_shadow_observe_only(state)

    # Use the formal enforcement contract from Phase 2.
    enforcement = shadow.enforce(state.shadow)
    if enforcement.action == "BLOCK":
        state.final_status = TerminalStatus.BLOCKED
        state.explanation = enforcement.explanation
        _record(
            state, node="shadow_audit", entered_at=entered_at,
            decision={
                "shadow_status": state.shadow.status.value,
                "trace_id": state.shadow.trace_id,
            },
            exit_verdict="red",
            policy_hits=list(state.shadow.policy_hits or []),
        )
        return state
    if enforcement.action == "ESCALATE":
        state.final_status = TerminalStatus.MANUAL_REVIEW_REQUIRED
        state.explanation = enforcement.explanation
        _record(
            state, node="shadow_audit", entered_at=entered_at,
            decision={
                "shadow_status": state.shadow.status.value,
                "trace_id": state.shadow.trace_id,
            },
            exit_verdict="yellow",
            policy_hits=list(state.shadow.policy_hits or []),
        )
        return state
    # PROCEED: leave final_status unset so routing continues
    _record(
        state, node="shadow_audit", entered_at=entered_at,
        decision={
            "shadow_status": state.shadow.status.value,
            "trace_id": state.shadow.trace_id,
        },
        exit_verdict="green",
        policy_hits=list(state.shadow.policy_hits or []),
    )
    return state


# ---------------------------------------------------------------------------
# select_recipe — constrained recipe proposal
# ---------------------------------------------------------------------------

def select_recipe(state: GraphState) -> GraphState:
    entered_at = _now_iso()
    backend = _backend(task="recipe")
    proposal = backend.propose_recipe(state)
    _drain_llm_trace(state, backend)

    if proposal is None:
        # No recipe matches this intent. Don't terminate here — shadow
        # may still RED-block (e.g. MASS_PRICING_ERROR is a compliance
        # outcome with no recipe by design). If shadow returns GREEN,
        # execute_recipe's guard on `state.invocation is None` will
        # raise FAIL_TO_HUMAN with the same explanation. This
        # preserves shadow's authority to be the terminal voice for
        # recipe-less intents — required by the 2026-04-22 reorder
        # which moved shadow_audit after select_recipe.
        state.explanation = "No deterministic recipe available for this intent."
        _record(
            state, node="select_recipe", entered_at=entered_at,
            decision={"recipe_name": None},
            exit_verdict="no_recipe",
        )
        return state

    state.selected_recipe = proposal.recipe_name
    _record(
        state, node="select_recipe", entered_at=entered_at,
        decision={"recipe_name": state.selected_recipe},
        exit_verdict="ok",
    )
    return state


# ---------------------------------------------------------------------------
# validate_types — build typed RecipeInvocation from event fields
# ---------------------------------------------------------------------------

def validate_types(state: GraphState) -> GraphState:
    entered_at = _now_iso()
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
        # Gateway-resolved context (Phase B + ADR-029). All three values
        # default to {} when gateways have not been called — keeps
        # standalone-test paths working without orchestration setup.
        #   fulfillment_status / matched_po_details — OMS reads (Phase B)
        #   tenant_config — 5-level config resolver (ADR-029); the recipe
        #     uses .get("weights") which returns None when absent so the
        #     recipe falls back to its module-default _WEIGHTS map.
        fulfillment = state.enrichment_context.get("fulfillment_status", {})
        matched_details = state.enrichment_context.get("matched_po_details", {})
        tenant_config = state.enrichment_context.get("tenant_config", {})
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
                "weights": tenant_config.get("weights"),
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
            _record(
                state, node="validate_types", entered_at=entered_at,
                decision={
                    "recipe": state.selected_recipe,
                    "guard": "sap_base_price_non_positive",
                    "sap_base_price": state.event.sap_base_price,
                },
                exit_verdict="invocation_fail",
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
    elif state.selected_recipe == "EmailOrderEntryRecipe.py":
        # ADR-034 Phase B — the email_intake gateway resolves the four
        # non-disable-able floor checks (sender_auth, resolve_customer,
        # duplicate_po_pre_check, credit_check) into enrichment_context
        # BEFORE shadow_audit (ADR-025). Prefer the gateway response;
        # fall back to event.metadata.non_disableable_floor when a
        # gateway response is empty (e.g. a soft gateway failure that
        # left an empty dict per resolve_dependencies' soft-fail path).
        meta = state.event.metadata
        floor_meta = meta.get("non_disableable_floor") or {}
        if not isinstance(floor_meta, dict):
            floor_meta = {}
        enrichment = state.enrichment_context
        gateway_floor: dict[str, bool] = {}
        for gw_key, flag, recipe_key in (
            ("sender_auth_context", "sender_authorized", "sender_authorized"),
            ("customer_resolution_context", "customer_resolved", "customer_resolved"),
            ("duplicate_po_pre_check_context", "duplicate_po_clear", "duplicate_po_clear"),
            ("credit_check_context", "credit_clear", "credit_clear"),
        ):
            gw = enrichment.get(gw_key)
            if isinstance(gw, dict) and flag in gw:
                gateway_floor[recipe_key] = bool(gw.get(flag))
            elif recipe_key in floor_meta:
                gateway_floor[recipe_key] = bool(floor_meta.get(recipe_key, False))
        failures = meta.get("validation_failures") or []
        if not isinstance(failures, list):
            failures = []
        state.invocation = RecipeInvocation(
            recipe_name=state.selected_recipe,
            params={
                "order_id": state.event.order_id,
                "customer_id": state.event.retailer_id or "",
                "composite_confidence": float(meta.get("composite_confidence") or 0.0),
                "validation_failures": failures,
                "non_disableable_floor": gateway_floor,
                "autonomy_levels": EMAIL_ORDER_ENTRY_AUTONOMY_LEVELS,
                "threshold_auto_approve": EMAIL_ORDER_AUTO_APPROVE_CONFIDENCE,
                "threshold_review_band_low": EMAIL_ORDER_REVIEW_BAND_LOW,
                "threshold_auto_correct": EMAIL_ORDER_AUTO_CORRECT_CONFIDENCE,
                "reject_reason_code": meta.get("reject_reason_code"),
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
        _record(
            state, node="validate_types", entered_at=entered_at,
            decision={"recipe": state.selected_recipe, "guard": "no_branch"},
            exit_verdict="invocation_fail",
        )
        return state
    _record(
        state, node="validate_types", entered_at=entered_at,
        decision={
            "recipe": state.selected_recipe,
            "param_keys": (
                sorted(state.invocation.params.keys())
                if state.invocation else []
            ),
        },
        exit_verdict="ok",
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
    entered_at = _now_iso()
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
    _record(
        state, node="explain_only", entered_at=entered_at,
        decision={
            "intent": intent_val,
            "shadow_verdict": shadow_verdict,
            "selected_recipe": state.selected_recipe,
        },
    )
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
    entered_at = _now_iso()
    if state.selected_recipe is None:
        _record(
            state, node="resolve_dependencies", entered_at=entered_at,
            decision={"recipe": None, "gateway_count": 0},
            exit_verdict="ok",
        )
        return state

    spec = get_recipe(state.selected_recipe)
    if not spec.dependencies:
        _record(
            state, node="resolve_dependencies", entered_at=entered_at,
            decision={"recipe": state.selected_recipe, "gateway_count": 0},
            exit_verdict="ok",
        )
        return state

    executor = GatewayExecutor()
    # Gateway-call correlation uses the request-scoped trace ID
    # (stamped at ingest) — distinct from the ComplianceDecision
    # trace ID, which only exists after shadow_audit runs. Falls
    # back to shadow.trace_id when present so re-resolution paths
    # post-shadow keep stable correlation.
    trace_id = state.request_trace_id or (
        state.shadow.trace_id if state.shadow else ""
    )

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

    sub_spans: list[GatewayCallSpan] = []
    # Track per-gateway start time keyed by future so we can compute
    # per-call duration even when futures complete out of submission
    # order.
    gateway_started_at: dict[concurrent.futures.Future, str] = {}

    # Resolve independent dependencies concurrently.
    future_to_dep = {}
    for dep, req in requests:
        future = executor._pool.submit(executor.run, req)
        future_to_dep[future] = dep
        gateway_started_at[future] = _now_iso()
    for future in concurrent.futures.as_completed(future_to_dep):
        dep = future_to_dep[future]
        response = future.result()
        finished_at = _now_iso()
        started_at = gateway_started_at[future]
        try:
            duration_ms = max(0, int(
                (datetime.fromisoformat(finished_at)
                 - datetime.fromisoformat(started_at)).total_seconds() * 1000
            ))
        except (TypeError, ValueError):
            duration_ms = None

        sub_spans.append(GatewayCallSpan(
            gateway=f"{dep.gateway_name}/{dep.operation}",
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            status=(
                "ok" if response.status == "SUCCESS"
                else ("timeout" if response.status == "TIMEOUT" else "error")
            ),
        ))

        if response.status != "SUCCESS":
            if dep.required_for_audit:
                # Strict: this evidence is audit-bearing; without it
                # the operator can't authorise the action. Halt.
                state.final_status = TerminalStatus.FAIL_TO_HUMAN
                state.explanation = (
                    f"Gateway dependency failed: "
                    f"{dep.gateway_name}/{dep.operation}"
                    f" — {response.error or response.status}"
                )
                _record(
                    state, node="resolve_dependencies", entered_at=entered_at,
                    decision={
                        "recipe": state.selected_recipe,
                        "gateway_count": len(spec.dependencies),
                        "failed_gateway": (
                            f"{dep.gateway_name}/{dep.operation}"
                        ),
                    },
                    exit_verdict="required_gw_fail",
                    sub_spans=sub_spans,
                )
                return state
            # Soft-fail: write an empty bag and continue. The
            # composer's coverage check will route to
            # AUDIT_CONTEXT_MISSING if the absent fields turn out to
            # be required by the registry; otherwise the run proceeds
            # normally.
            _node_logger.warning(
                "gateway_dependency_soft_fail",
                extra={
                    "gateway": dep.gateway_name,
                    "operation": dep.operation,
                    "result_key": dep.result_key,
                    "error": response.error or response.status,
                },
            )
            state.enrichment_context[dep.result_key] = {}
            continue

        state.enrichment_context[dep.result_key] = response.data

    _record(
        state, node="resolve_dependencies", entered_at=entered_at,
        decision={
            "recipe": state.selected_recipe,
            "gateway_count": len(spec.dependencies),
        },
        exit_verdict="ok",
        sub_spans=sub_spans,
    )
    return state


# ---------------------------------------------------------------------------
# execute_recipe — delegate to RecipeExecutor (Phase 3)
# ---------------------------------------------------------------------------

def execute_recipe(state: GraphState) -> GraphState:
    entered_at = _now_iso()
    # Explicit guard — assert is disabled in optimized Python (-O flag).
    if state.invocation is None:
        state.final_status = TerminalStatus.FAIL_TO_HUMAN
        # Preserve any upstream explanation (e.g. select_recipe's
        # "No deterministic recipe available for this intent.") —
        # only synthesise one when nothing was set earlier.
        if not state.explanation:
            state.explanation = (
                "No recipe invocation available; "
                "validate_types may have found no recipe."
            )
        _record(
            state, node="execute_recipe", entered_at=entered_at,
            decision={"recipe": None, "guard": "no_invocation"},
        )
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
        _record(
            state, node="execute_recipe", entered_at=entered_at,
            decision={
                "recipe": state.invocation.recipe_name,
                "errors": list(log.errors),
            },
        )
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

    _record(
        state, node="execute_recipe", entered_at=entered_at,
        decision={
            "recipe": state.invocation.recipe_name,
            "recipe_status": recipe_status,
            "autonomy_level": autonomy,
            "final_status": (
                state.final_status.value if state.final_status else None
            ),
        },
    )
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
    entered_at = _now_iso()
    if state.selected_recipe is None or state.execution_log is None:
        _record(
            state, node="apply_effects", entered_at=entered_at,
            decision={"recipe": state.selected_recipe, "effect_count": 0},
        )
        return state

    spec = get_recipe(state.selected_recipe)
    if not spec.effects:
        _record(
            state, node="apply_effects", entered_at=entered_at,
            decision={"recipe": state.selected_recipe, "effect_count": 0},
        )
        return state

    executor = GatewayExecutor()
    # Effects run post-shadow-GREEN, so shadow.trace_id is always set.
    # Prefer it for continuity with the audit decision; fall back to
    # request_trace_id for defensive coverage.
    trace_id = (
        state.shadow.trace_id
        if state.shadow else state.request_trace_id
    )

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

    _record(
        state, node="apply_effects", entered_at=entered_at,
        decision={
            "recipe": state.selected_recipe,
            "effect_count": len(state.effect_results),
            "effects_ok": sum(
                1 for r in state.effect_results if r.status == "SUCCESS"
            ),
        },
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
    entered_at = _now_iso()
    # Defensive re-entry guard: if a prior node already set
    # AUDIT_CONTEXT_MISSING, leave it alone. Don't double-append
    # missing-field lists.
    if state.final_status == TerminalStatus.AUDIT_CONTEXT_MISSING:
        _record(
            state, node="build_analysis", entered_at=entered_at,
            decision={
                "final_status": state.final_status.value,
                "guard": "audit_context_missing_re_entry",
            },
        )
        return state
    # FAIL_TO_HUMAN is the system's "this exception needs human
    # investigation outside the normal flow" terminal — typically
    # set by validate_circuit_breaker, an unrecoverable input
    # validation failure, or a missing recipe. Audit-completeness
    # checks are moot when the record isn't going to a normal
    # operator review surface; preserve FAIL_TO_HUMAN so circuit
    # breaker / validation failures stay debuggable.
    if state.final_status == TerminalStatus.FAIL_TO_HUMAN:
        _record(
            state, node="build_analysis", entered_at=entered_at,
            decision={
                "final_status": state.final_status.value,
                "guard": "fail_to_human_skip",
            },
        )
        return state

    # ADR-028 G1 / action item A5 — DUPLICATE_PO metadata-contract gate.
    # Validate the input event metadata + recipe output shape against
    # the contract documented in
    # docs/specs/duplicate-po/metadata-contract.md. On violation, route
    # to AUDIT_CONTEXT_MISSING with a per-key offender explanation —
    # same routing the registry-coverage check below uses, so auditors
    # see "compliance data was malformed" rather than a Pydantic
    # ValidationError stack trace. Runs BEFORE the registry-coverage
    # check so a contract violation surfaces as the precise root cause
    # rather than as a downstream missing-field symptom.
    if state.intent == Intent.DUPLICATE_PO:
        # Lazy import — same isolation reason as the composer below.
        from contracts.duplicate_po_contract import (
            MetadataContractViolation,
            validate_duplicate_po_event_metadata,
            validate_duplicate_po_recipe_output,
        )

        contract_offenders: list[tuple[str, str]] = []
        contract_name: str | None = None
        try:
            validate_duplicate_po_event_metadata(state.event.metadata)
        except MetadataContractViolation as exc:
            contract_offenders.extend(exc.offenders)
            contract_name = exc.contract_name

        # Recipe-output validation only runs when the recipe actually
        # executed (execution_log present + outputs populated). Earlier
        # halt paths (BLOCKED by shadow, no_recipe, etc.) leave
        # execution_log empty; those records are legitimately absent
        # the recipe-output dict and shouldn't trigger a violation.
        if (
            state.execution_log is not None
            and state.execution_log.outputs
            and state.selected_recipe == "DuplicatePORecipe.py"
        ):
            try:
                validate_duplicate_po_recipe_output(
                    state.execution_log.outputs,
                )
            except MetadataContractViolation as exc:
                contract_offenders.extend(exc.offenders)
                # Output-side contract takes precedence in the
                # explanation when both fail — operators are more
                # likely to act on the recipe-side issue.
                contract_name = exc.contract_name

        if contract_offenders:
            offender_summary = "; ".join(
                f"{key}: {reason}" for key, reason in contract_offenders
            )
            preamble = state.explanation or ""
            suffix = (
                f"Metadata-contract violation in {contract_name}: "
                f"[{offender_summary}]. See "
                f"docs/specs/duplicate-po/metadata-contract.md for "
                f"the binding key/type rules."
            )
            state.final_status = TerminalStatus.AUDIT_CONTEXT_MISSING
            state.explanation = (
                f"{preamble}\n\n{suffix}" if preamble else suffix
            )
            _record(
                state, node="build_analysis", entered_at=entered_at,
                decision={
                    "final_status": state.final_status.value,
                    "audit_coverage": "metadata_contract_violation",
                    "contract": contract_name,
                    "offenders": [
                        {"key": k, "reason": r}
                        for k, r in contract_offenders
                    ],
                },
            )
            return state

    # Lazy import — keeps orchestration independent of the API layer
    # in the import graph so test isolation stays clean.
    from api.analysis_composer import compose_from_state

    composed = compose_from_state(state)

    if not composed.should_route_to_audit_context_missing:
        _record(
            state, node="build_analysis", entered_at=entered_at,
            decision={
                "final_status": (
                    state.final_status.value if state.final_status else None
                ),
                "audit_coverage": "complete",
            },
        )
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
    _record(
        state, node="build_analysis", entered_at=entered_at,
        decision={
            "final_status": state.final_status.value,
            "audit_coverage": "incomplete",
            "missing_class": composed.class_name,
            "missing_fields": list(composed.missing_audit_fields),
        },
    )
    return state
