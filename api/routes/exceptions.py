"""Exception management endpoints (architecture_v3.md Section 8.2).

POST  /api/v1/exceptions/resolve         — synchronous resolution
POST  /api/v1/exceptions/resolve/async   — async resolution (stub)
POST  /api/v1/exceptions/resolve/explain — explain mode dry-run
GET   /api/v1/exceptions                 — paginated exception queue
GET   /api/v1/exceptions/stats           — dashboard metrics
GET   /api/v1/exceptions/{id}            — exception detail
GET   /api/v1/exceptions/{id}/trace      — full TraceRecord
PATCH /api/v1/exceptions/{id}/override   — YELLOW: override agent recommendation (manager+)
POST  /api/v1/exceptions/{id}/approve    — resume paused exception (manager+)
POST  /api/v1/exceptions/{id}/reject     — reject paused exception (manager+)
POST  /api/v1/exceptions/{id}/challenge  — GREEN: post-execution challenge (analyst+)
POST  /api/v1/exceptions/{id}/admin-release — RED: admin release of blocked exception (admin)

Three-tier human intervention model:
  GREEN  → Post-execution challenge (RESOLVED → ESCALATED)
  YELLOW → Override recommendation (PENDING_REVIEW → RESOLVED)
  RED    → Admin release (BLOCKED → PENDING_ADMIN_REVIEW)

Security:
  §11.3 — Partner-role users see only their own orders (retailer_id filtering)
  §11.4 — X-Trace-ID propagated from request header into graph execution
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request

from api.deps import AuthenticatedUser, get_current_user, get_tenant_id, require_role
from api.errors import ASOEError
from api.schemas import (
    AdminReleaseRequest,
    AnalysisResponse,
    ApproveRequest,
    AsyncResolveResponse,
    ChallengeRequest,
    ExceptionDetailResponse,
    ExceptionListResponse,
    LineAnalysis,
    LineItem,
    LineItemsResponse,
    OverrideRequest,
    ReanalyzeRequest,
    RejectRequest,
    ResolveRequest,
    ResolveResponse,
    StatsResponse,
    TraceResponse,
)
from contracts.policy import REANALYSIS_MAX_ATTEMPTS
from api.events import WSEvent
from api.pubsub import event_publisher
from api.store import exception_store
from constraints.specs import AllowedResolutionAction
from contracts.models import (
    ADMIN_RELEASE_SOURCE_STATES,
    CHALLENGE_SOURCE_STATES,
    HITL_APPROVE_STATES,
    HITL_OVERRIDE_STATES,
    HITL_REJECT_STATES,
    GraphState,
    OrderEvent,
)

logger = logging.getLogger("asoe.api.exceptions")

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_404(exception_id: str, tenant_id: str):
    """Fetch an exception record or raise a 404 ASOEError."""
    record = exception_store.get(exception_id, tenant_id)
    if not record:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"Exception {exception_id} not found.",
            status_code=404,
        )
    return record


def _require_state(record, allowed_states: set, action: str) -> None:
    """Raise 409 if the exception is not in one of the allowed states."""
    if record.lifecycle_state not in allowed_states:
        allowed_str = ", ".join(sorted(allowed_states))
        raise ASOEError(
            code="INVALID_STATE",
            message=(
                f"Cannot {action}: exception is in state "
                f"'{record.lifecycle_state}', expected one of: {allowed_str}."
            ),
            status_code=409,
        )


def _require_pending_review(record) -> None:
    """Raise 409 if the exception is not in a HITL-approvable state."""
    _require_state(record, HITL_APPROVE_STATES, "approve/reject")


def _get_trace_id(request: Request) -> str:
    """Extract trace_id from middleware state (§11.4)."""
    return getattr(request.state, "trace_id", str(uuid4()))


def _build_order_event(req: ResolveRequest) -> OrderEvent:
    """Construct an OrderEvent from the API request body."""
    return OrderEvent.model_validate(req.model_dump())


def _run_graph_safe(state: GraphState, *, explain_mode: bool | None = None) -> GraphState:
    """Run the graph with proper error handling."""
    from orchestration.graph import run_graph
    return run_graph(state, explain_mode=explain_mode)


def _state_to_resolve_response(
    exception_id: str, state: GraphState,
) -> ResolveResponse:
    trace_id = None
    if state.shadow:
        trace_id = state.shadow.trace_id
    elif state.execution_log:
        trace_id = state.execution_log.trace_id

    execution_log_dict = None
    if state.execution_log:
        execution_log_dict = state.execution_log.model_dump()

    return ResolveResponse(
        exception_id=exception_id,
        trace_id=trace_id,
        intent=state.intent.value if state.intent else None,
        shadow_verdict=state.shadow.status.value if state.shadow else None,
        selected_recipe=state.selected_recipe,
        final_status=state.final_status.value if state.final_status else None,
        explanation=state.explanation,
        execution_log=execution_log_dict,
        effect_results=[r.model_dump() for r in state.effect_results],
    )


def _persist_exception(
    tenant_id: str, state: GraphState, trace_id: Optional[str],
) -> str:
    """Store exception record and trace data. Returns exception_id."""
    record = exception_store.create(
        tenant_id=tenant_id,
        order_id=state.event.order_id,
        event_type=state.event.event_type,
        trace_id=trace_id or str(uuid4()),
        intent=state.intent.value if state.intent else None,
        shadow_verdict=state.shadow.status.value if state.shadow else None,
        selected_recipe=state.selected_recipe,
        final_status=state.final_status.value if state.final_status else None,
        resolution_data=state.execution_log.outputs if state.execution_log else {},
        # Capture the source event so a future re-analysis can replay it
        # through the graph without relying on external state reconstruction.
        original_event=state.event.model_dump(mode="json"),
    )

    trace_data = {
        "trace_id": trace_id or "",
        "event_id": state.event.order_id,
        "skill_name": state.skill.name if state.skill else None,
        "intent_selected": state.intent.value if state.intent else None,
        "shadow_verdict": state.shadow.status.value if state.shadow else None,
        "shadow_policy_hits": state.shadow.policy_hits if state.shadow else [],
        "recipe_name": state.selected_recipe,
        "constrained_output_schemas": state.execution_log.constrained_outputs if state.execution_log else {},
        "gateway_calls": [],
        "backend_fallback": "deterministic_fallback",
        "is_fallback_generated": True,
        "final_status": state.final_status.value if state.final_status else None,
        "explanation": state.explanation,
    }
    exception_store.store_trace(record.id, trace_data)
    return record.id


def _publish_task_complete(
    tenant_id: str,
    exception_id: str,
    trace_id: str,
    state: GraphState,
    task_id: Optional[str] = None,
) -> None:
    """Publish a task_complete event to the pub/sub channel (§10)."""
    final = state.final_status.value if state.final_status else "UNKNOWN"
    event = WSEvent.task_complete(
        trace_id=trace_id,
        exception_id=exception_id,
        tenant_id=tenant_id,
        task_id=task_id or exception_id,
        final_status=final,
        explanation=state.explanation,
    )
    event_publisher.publish(tenant_id, event)


# ---------------------------------------------------------------------------
# POST /api/v1/exceptions/resolve — Synchronous resolution
# ---------------------------------------------------------------------------

@router.post(
    "/exceptions/resolve",
    response_model=ResolveResponse,
    dependencies=[Depends(require_role("analyst", "manager", "admin"))],
)
async def resolve(
    request: Request,
    req: ResolveRequest,
    tenant_id: str = Depends(get_tenant_id),
) -> ResolveResponse:
    trace_id = _get_trace_id(request)
    event = _build_order_event(req)
    state = GraphState(event=event)

    try:
        final_state = _run_graph_safe(state)
    except Exception as exc:
        logger.error("Graph execution failed: %s", exc)
        raise ASOEError(
            code="GRAPH_EXECUTION_ERROR",
            message=f"Graph execution failed: {exc}",
            status_code=500,
            trace_id=trace_id,
        )

    graph_trace_id = final_state.shadow.trace_id if final_state.shadow else trace_id
    exception_id = _persist_exception(tenant_id, final_state, graph_trace_id)
    _publish_task_complete(tenant_id, exception_id, graph_trace_id, final_state)
    return _state_to_resolve_response(exception_id, final_state)


# ---------------------------------------------------------------------------
# POST /api/v1/exceptions/resolve/async — Async resolution (stub)
# ---------------------------------------------------------------------------

@router.post(
    "/exceptions/resolve/async",
    response_model=AsyncResolveResponse,
    dependencies=[Depends(require_role("analyst", "manager", "admin"))],
)
async def resolve_async(
    request: Request,
    req: ResolveRequest,
    tenant_id: str = Depends(get_tenant_id),
) -> AsyncResolveResponse:
    task_id = str(uuid4())
    event = _build_order_event(req)
    state = GraphState(event=event)

    try:
        final_state = _run_graph_safe(state)
    except Exception as exc:
        logger.error("Async graph execution failed: %s", exc)
        raise ASOEError(
            code="GRAPH_EXECUTION_ERROR",
            message=f"Graph execution failed: {exc}",
            status_code=500,
            trace_id=_get_trace_id(request),
        )

    trace_id = final_state.shadow.trace_id if final_state.shadow else None
    exc_id = _persist_exception(tenant_id, final_state, trace_id)
    _publish_task_complete(tenant_id, exc_id, trace_id or "", final_state, task_id=task_id)
    return AsyncResolveResponse(task_id=task_id, status="complete")


# ---------------------------------------------------------------------------
# POST /api/v1/exceptions/resolve/explain — Explain mode dry-run
# ---------------------------------------------------------------------------

@router.post(
    "/exceptions/resolve/explain",
    response_model=ResolveResponse,
    dependencies=[Depends(require_role("analyst", "manager", "admin"))],
)
async def resolve_explain(
    request: Request,
    req: ResolveRequest,
    tenant_id: str = Depends(get_tenant_id),
) -> ResolveResponse:
    event = _build_order_event(req)
    state = GraphState(event=event)

    try:
        final_state = _run_graph_safe(state, explain_mode=True)
    except Exception as exc:
        logger.error("Explain graph execution failed: %s", exc)
        raise ASOEError(
            code="GRAPH_EXECUTION_ERROR",
            message=f"Graph execution failed: {exc}",
            status_code=500,
            trace_id=_get_trace_id(request),
        )

    trace_id = final_state.shadow.trace_id if final_state.shadow else None
    exception_id = _persist_exception(tenant_id, final_state, trace_id)
    _publish_task_complete(tenant_id, exception_id, trace_id or "", final_state)
    return _state_to_resolve_response(exception_id, final_state)


# ---------------------------------------------------------------------------
# GET /api/v1/exceptions — Paginated exception queue
# ---------------------------------------------------------------------------

@router.get(
    "/exceptions",
    response_model=ExceptionListResponse,
    dependencies=[Depends(require_role("analyst", "manager", "admin", "viewer", "partner"))],
)
async def list_exceptions(
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
    status: Optional[str] = Query(None, description="Filter by lifecycle state"),
    intent: Optional[str] = Query(None, description="Filter by intent"),
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None, description="Pagination cursor"),
) -> ExceptionListResponse:
    records, next_cursor, has_more = exception_store.list(
        tenant_id=tenant_id,
        status=status,
        intent=intent,
        limit=limit,
        cursor=cursor,
    )

    # Unified account scoping: assigned_accounts (internal) and retailer_id (partner)
    # are enforced server-side using the same mechanism.
    if user.assigned_accounts:
        # Internal users scoped to specific accounts — filter by account_id
        allowed = set(user.assigned_accounts)
        records = [r for r in records if getattr(r, "account_id", None) in allowed]
    elif "partner" in user.roles and user.retailer_id:
        # Partner-role legacy: filter by order_id prefix (pre-account_id compat)
        records = [r for r in records if getattr(r, "order_id", "").startswith(user.retailer_id)]

    return ExceptionListResponse(
        data=[r.to_summary() for r in records],
        cursor=next_cursor,
        has_more=has_more,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/exceptions/stats — Dashboard metrics
# ---------------------------------------------------------------------------

@router.get(
    "/exceptions/stats",
    response_model=StatsResponse,
    dependencies=[Depends(require_role("analyst", "manager", "admin", "viewer"))],
)
async def stats(tenant_id: str = Depends(get_tenant_id)) -> StatsResponse:
    s = exception_store.stats(tenant_id)
    return StatsResponse(**s)


# ---------------------------------------------------------------------------
# GET /api/v1/exceptions/{id} — Exception detail
# ---------------------------------------------------------------------------

@router.get(
    "/exceptions/{exception_id}",
    response_model=ExceptionDetailResponse,
    dependencies=[Depends(require_role("analyst", "manager", "admin", "viewer", "partner"))],
)
async def get_exception(
    exception_id: str,
    tenant_id: str = Depends(get_tenant_id),
) -> ExceptionDetailResponse:
    return _get_or_404(exception_id, tenant_id).to_detail()


# ---------------------------------------------------------------------------
# GET /api/v1/exceptions/{id}/trace — Full TraceRecord
# ---------------------------------------------------------------------------

@router.get(
    "/exceptions/{exception_id}/trace",
    response_model=TraceResponse,
    dependencies=[Depends(require_role("analyst", "manager", "admin"))],
)
async def get_trace(
    exception_id: str,
    tenant_id: str = Depends(get_tenant_id),
) -> TraceResponse:
    _get_or_404(exception_id, tenant_id)
    trace_data = exception_store.get_trace(exception_id)
    if not trace_data:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"Trace data for exception {exception_id} not found.",
            status_code=404,
        )
    return TraceResponse(**trace_data)


# ---------------------------------------------------------------------------
# PATCH /api/v1/exceptions/{id}/override — Human override
# ---------------------------------------------------------------------------

@router.patch(
    "/exceptions/{exception_id}/override",
    response_model=ExceptionDetailResponse,
    dependencies=[Depends(require_role("manager", "admin"))],
)
async def override_exception(
    exception_id: str,
    req: OverrideRequest,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ExceptionDetailResponse:
    """YELLOW-tier HITL: override the agent's recommended action.

    Restricted to PENDING_REVIEW and ESCALATED states.
    Action must be a valid AllowedResolutionAction.
    Notes are mandatory (SOX audit requirement).
    """
    record = _get_or_404(exception_id, tenant_id)
    _require_state(record, HITL_OVERRIDE_STATES, "override")

    # Validate action against constrained vocabulary
    allowed = list(AllowedResolutionAction.__args__)  # type: ignore[attr-defined]
    if req.action not in allowed:
        raise ASOEError(
            code="INVALID_ACTION",
            message=f"Action '{req.action}' is not allowed. Valid: {allowed}",
            status_code=422,
        )

    updated = exception_store.update(
        exception_id,
        tenant_id,
        resolved_by=req.resolved_by or user.sub,
        resolved_action=req.action,
        resolution_notes=req.notes,
        lifecycle_state="RESOLVED",
        final_status="COMPLETE",
    )
    if not updated:
        raise ASOEError(
            code="UPDATE_FAILED",
            message="Failed to update exception.",
            status_code=500,
        )

    # SOX audit: log override to policy_audit_log
    exception_store.log_audit_event(
        tenant_id=tenant_id,
        policy_key="EXCEPTION_OVERRIDE",
        previous_value={"lifecycle_state": record.lifecycle_state,
                        "recommended_action": record.resolution_data.get("recommended_action")},
        new_value={"resolved_action": req.action, "exception_id": exception_id},
        changed_by=req.resolved_by or user.sub,
        change_reason=req.notes,
    )

    return updated.to_detail()


# ---------------------------------------------------------------------------
# POST /api/v1/exceptions/{id}/reanalyze — Human-triggered graph replay
# ---------------------------------------------------------------------------

# Lifecycle/verdict states eligible for reanalysis. Allowed only when the
# prior outcome was non-terminal-success — preventing re-runs of GREEN
# auto-resolved exceptions (which would indicate outcome-shopping) and of
# already-closed exceptions.
REANALYZE_ELIGIBLE_VERDICTS = {"YELLOW", "RED"}
REANALYZE_ELIGIBLE_LIFECYCLES = {
    "PENDING_REVIEW", "ESCALATED", "PENDING_ADMIN_REVIEW", "BLOCKED", "FAILED",
}


@router.post(
    "/exceptions/{exception_id}/reanalyze",
    response_model=ExceptionDetailResponse,
    dependencies=[Depends(require_role("manager", "admin"))],
)
async def reanalyze_exception(
    exception_id: str,
    req: ReanalyzeRequest,
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ExceptionDetailResponse:
    """Replay an exception through the full graph with a fresh Compliance
    Shadow decision.

    Governance (mirrors CLAUDE.md §4):
    - Re-run flows through the same run_graph() pipeline — Compliance
      Shadow is NOT bypassed.
    - Eligible only for non-terminal outcomes where human review is already
      warranted (YELLOW/RED verdict, or FAILED/BLOCKED/ESCALATED/PENDING*).
    - Rate-limited per-exception (REANALYSIS_MAX_ATTEMPTS in
      contracts/policy.py) to prevent outcome-shopping.
    - Prior and new outcomes are preserved immutably in reanalysis_history
      and in the policy audit log (SOX).
    """
    record = _get_or_404(exception_id, tenant_id)

    # Eligibility gate — either the prior verdict needs review, or the
    # prior run crashed/was blocked. GREEN + RESOLVED is ineligible.
    verdict_eligible = record.shadow_verdict in REANALYZE_ELIGIBLE_VERDICTS
    lifecycle_eligible = record.lifecycle_state in REANALYZE_ELIGIBLE_LIFECYCLES
    if not (verdict_eligible or lifecycle_eligible):
        raise ASOEError(
            code="INVALID_STATE",
            message=(
                f"Cannot reanalyze: exception verdict is "
                f"'{record.shadow_verdict}' and lifecycle is "
                f"'{record.lifecycle_state}'. Reanalysis is permitted only "
                f"on YELLOW/RED verdicts or on FAILED/BLOCKED/ESCALATED "
                f"lifecycles."
            ),
            status_code=409,
        )

    # Rate limit — bound total re-runs per exception.
    attempts_so_far = len(record.reanalysis_history)
    if attempts_so_far >= REANALYSIS_MAX_ATTEMPTS:
        raise ASOEError(
            code="RATE_LIMITED",
            message=(
                f"Reanalysis limit reached ({REANALYSIS_MAX_ATTEMPTS} attempts). "
                f"Escalate to admin for manual resolution."
            ),
            status_code=429,
        )

    # Replay requires the original event. If a record pre-dates the
    # feature, there's nothing to replay — surface explicitly rather than
    # reconstructing partial data.
    if not record.original_event:
        raise ASOEError(
            code="REPLAY_UNAVAILABLE",
            message=(
                "Cannot reanalyze: the original event is not on file for "
                "this exception. Reanalysis requires the source event."
            ),
            status_code=409,
        )

    # Re-run the graph — same pipeline, fresh Compliance Shadow.
    trace_id = _get_trace_id(request)
    try:
        event = OrderEvent.model_validate(record.original_event)
    except Exception as exc:
        raise ASOEError(
            code="REPLAY_UNAVAILABLE",
            message=f"Stored event failed validation: {exc}",
            status_code=409,
            trace_id=trace_id,
        )

    state = GraphState(event=event)
    try:
        final_state = _run_graph_safe(state)
    except Exception as exc:
        logger.error("Reanalyze graph execution failed: %s", exc)
        raise ASOEError(
            code="GRAPH_EXECUTION_ERROR",
            message=f"Graph execution failed during reanalysis: {exc}",
            status_code=500,
            trace_id=trace_id,
        )

    new_trace_id = (
        final_state.shadow.trace_id if final_state.shadow else trace_id
    )
    new_verdict = (
        final_state.shadow.status.value if final_state.shadow else None
    )
    new_final_status = (
        final_state.final_status.value if final_state.final_status else None
    )
    new_lifecycle = (
        # Reuse the same status→lifecycle mapping the resolve endpoint uses.
        _resolve_lifecycle(new_final_status)
    )

    # Capture the prior outcome before mutating so the audit trail is exact.
    prior_entry = {
        "attempt": attempts_so_far + 1,
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "triggered_by": user.sub,
        "reason": req.reason,
        "prior_trace_id": record.trace_id,
        "prior_shadow_verdict": record.shadow_verdict,
        "prior_final_status": record.final_status,
        "prior_lifecycle_state": record.lifecycle_state,
        "new_trace_id": new_trace_id,
        "new_shadow_verdict": new_verdict,
        "new_final_status": new_final_status,
        "new_lifecycle_state": new_lifecycle,
    }

    # Persist the new trace data, then update the record fields, then
    # append to reanalysis_history. Order matters: history must reflect the
    # new state already written to the record.
    trace_data = {
        "trace_id": new_trace_id,
        "event_id": final_state.event.order_id,
        "skill_name": final_state.skill.name if final_state.skill else None,
        "intent_selected": final_state.intent.value if final_state.intent else None,
        "shadow_verdict": new_verdict,
        "shadow_policy_hits": final_state.shadow.policy_hits if final_state.shadow else [],
        "recipe_name": final_state.selected_recipe,
        "constrained_output_schemas": (
            final_state.execution_log.constrained_outputs
            if final_state.execution_log else {}
        ),
        "gateway_calls": [],
        "backend_fallback": "deterministic_fallback",
        "is_fallback_generated": True,
        "final_status": new_final_status,
        "explanation": final_state.explanation,
    }
    exception_store.store_trace(exception_id, trace_data)

    updated = exception_store.update(
        exception_id,
        tenant_id,
        trace_id=new_trace_id,
        intent=final_state.intent.value if final_state.intent else record.intent,
        shadow_verdict=new_verdict,
        selected_recipe=final_state.selected_recipe,
        final_status=new_final_status,
        lifecycle_state=new_lifecycle,
    )
    if not updated:
        raise ASOEError(
            code="UPDATE_FAILED",
            message="Failed to update exception after reanalysis.",
            status_code=500,
        )

    updated = exception_store.append_reanalysis(
        exception_id, tenant_id, prior_entry,
    )
    if not updated:
        raise ASOEError(
            code="UPDATE_FAILED",
            message="Failed to persist reanalysis history.",
            status_code=500,
        )

    # SOX audit — log reanalysis alongside other human governance actions.
    exception_store.log_audit_event(
        tenant_id=tenant_id,
        policy_key="EXCEPTION_REANALYZE",
        previous_value={
            "shadow_verdict": prior_entry["prior_shadow_verdict"],
            "final_status": prior_entry["prior_final_status"],
            "lifecycle_state": prior_entry["prior_lifecycle_state"],
            "trace_id": prior_entry["prior_trace_id"],
            "exception_id": exception_id,
        },
        new_value={
            "shadow_verdict": new_verdict,
            "final_status": new_final_status,
            "lifecycle_state": new_lifecycle,
            "trace_id": new_trace_id,
            "attempt": prior_entry["attempt"],
        },
        changed_by=user.sub,
        change_reason=req.reason,
    )

    # Publish task_complete over pub/sub so subscribed clients refresh.
    _publish_task_complete(tenant_id, exception_id, new_trace_id, final_state)

    return updated.to_detail()


def _resolve_lifecycle(final_status: Optional[str]) -> str:
    """Map a final_status to the persisted lifecycle_state.

    Re-uses the same mapping the store.create() helper applies so that a
    reanalysis produces the same lifecycle the initial resolve would have.
    """
    from contracts.models import STATUS_TO_LIFECYCLE
    return STATUS_TO_LIFECYCLE.get(final_status or "", "INGESTED")


# ---------------------------------------------------------------------------
# POST /api/v1/exceptions/{id}/approve — Resume paused exception
# ---------------------------------------------------------------------------

@router.post(
    "/exceptions/{exception_id}/approve",
    response_model=ExceptionDetailResponse,
    dependencies=[Depends(require_role("manager", "admin"))],
)
async def approve_exception(
    exception_id: str,
    req: ApproveRequest,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ExceptionDetailResponse:
    record = _get_or_404(exception_id, tenant_id)
    _require_pending_review(record)
    updated = exception_store.update(
        exception_id,
        tenant_id,
        lifecycle_state="EXECUTING",
        resolved_by=user.sub,
        resolution_notes=req.notes,
    )
    if not updated:
        raise ASOEError(code="UPDATE_FAILED", message="Failed to update.", status_code=500)
    return updated.to_detail()


# ---------------------------------------------------------------------------
# POST /api/v1/exceptions/{id}/reject — Reject paused exception
# ---------------------------------------------------------------------------

@router.post(
    "/exceptions/{exception_id}/reject",
    response_model=ExceptionDetailResponse,
    dependencies=[Depends(require_role("manager", "admin"))],
)
async def reject_exception(
    exception_id: str,
    req: RejectRequest,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ExceptionDetailResponse:
    record = _get_or_404(exception_id, tenant_id)
    _require_pending_review(record)
    updated = exception_store.update(
        exception_id,
        tenant_id,
        lifecycle_state="REJECTED",
        final_status="REJECTED",
        resolved_by=user.sub,
        resolution_notes=req.reason,
    )
    if not updated:
        raise ASOEError(code="UPDATE_FAILED", message="Failed to update.", status_code=500)
    return updated.to_detail()


# ---------------------------------------------------------------------------
# POST /api/v1/exceptions/{id}/challenge — Post-execution challenge (GREEN tier)
# ---------------------------------------------------------------------------

@router.post(
    "/exceptions/{exception_id}/challenge",
    response_model=ExceptionDetailResponse,
    dependencies=[Depends(require_role("analyst", "manager", "admin"))],
)
async def challenge_exception(
    exception_id: str,
    req: ChallengeRequest,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ExceptionDetailResponse:
    """GREEN-tier HITL: challenge a resolved exception for review.

    Transitions RESOLVED → ESCALATED for investigation.
    Does NOT undo executed actions (gateway effects already applied).
    """
    record = _get_or_404(exception_id, tenant_id)
    _require_state(record, CHALLENGE_SOURCE_STATES, "challenge")

    updated = exception_store.update(
        exception_id,
        tenant_id,
        lifecycle_state="ESCALATED",
        resolution_notes=f"CHALLENGED: {req.challenge_reason}",
    )
    if not updated:
        raise ASOEError(code="UPDATE_FAILED", message="Failed to update.", status_code=500)

    # SOX audit: log challenge event
    exception_store.log_audit_event(
        tenant_id=tenant_id,
        policy_key="EXCEPTION_CHALLENGE",
        previous_value={"lifecycle_state": "RESOLVED", "exception_id": exception_id},
        new_value={"lifecycle_state": "ESCALATED", "challenge_reason": req.challenge_reason},
        changed_by=user.sub,
        change_reason=req.challenge_reason,
    )

    return updated.to_detail()


# ---------------------------------------------------------------------------
# POST /api/v1/exceptions/{id}/admin-release — Admin release (RED tier)
# ---------------------------------------------------------------------------

@router.post(
    "/exceptions/{exception_id}/admin-release",
    response_model=ExceptionDetailResponse,
    dependencies=[Depends(require_role("admin"))],
)
async def admin_release_exception(
    exception_id: str,
    req: AdminReleaseRequest,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ExceptionDetailResponse:
    """RED-tier HITL: admin releases a RED-blocked exception.

    Transitions BLOCKED → PENDING_ADMIN_REVIEW.
    The admin must then approve, override, or reject the exception.
    The original RED verdict is preserved in the TraceRecord.
    risk_acknowledgment must be True (explicit risk acceptance).
    """
    record = _get_or_404(exception_id, tenant_id)
    _require_state(record, ADMIN_RELEASE_SOURCE_STATES, "admin-release")

    if not req.risk_acknowledgment:
        raise ASOEError(
            code="RISK_NOT_ACKNOWLEDGED",
            message="risk_acknowledgment must be true to release a RED-blocked exception.",
            status_code=422,
        )

    updated = exception_store.update(
        exception_id,
        tenant_id,
        lifecycle_state="PENDING_ADMIN_REVIEW",
        resolution_notes=f"ADMIN_RELEASE: {req.release_reason}",
    )
    if not updated:
        raise ASOEError(code="UPDATE_FAILED", message="Failed to update.", status_code=500)

    # SOX audit: log admin release to policy_audit_log (immutable)
    exception_store.log_audit_event(
        tenant_id=tenant_id,
        policy_key="EXCEPTION_ADMIN_RELEASE",
        previous_value={"lifecycle_state": "BLOCKED", "shadow_verdict": record.shadow_verdict,
                        "exception_id": exception_id},
        new_value={"lifecycle_state": "PENDING_ADMIN_REVIEW",
                   "release_reason": req.release_reason,
                   "risk_acknowledged": True},
        changed_by=user.sub,
        change_reason=req.release_reason,
    )

    return updated.to_detail()


# ---------------------------------------------------------------------------
# GET /api/v1/exceptions/{id}/line-items — Line items for an exception
# ---------------------------------------------------------------------------

@router.get(
    "/exceptions/{exception_id}/line-items",
    response_model=LineItemsResponse,
    dependencies=[Depends(require_role("analyst", "manager", "admin"))],
)
async def get_line_items(
    exception_id: str,
    tenant_id: str = Depends(get_tenant_id),
) -> LineItemsResponse:
    record = _get_or_404(exception_id, tenant_id)
    items: list[LineItem] = []

    # Extract line items from resolution_data or event metadata
    raw_items = record.resolution_data.get("line_items", [])
    for li in raw_items:
        items.append(
            LineItem(
                line_id=li.get("line_id", ""),
                sku=li.get("sku", ""),
                description=li.get("description", ""),
                uom=li.get("uom", "EA"),
                quantity=li.get("quantity", 0),
                erp_price=li.get("erp_price", 0.0),
                po_price=li.get("po_price", 0.0),
                root_cause=li.get("root_cause"),
            )
        )

    return LineItemsResponse(data=items)


# ---------------------------------------------------------------------------
# GET /api/v1/exceptions/{id}/analysis — Analysis for an exception
# ---------------------------------------------------------------------------

@router.get(
    "/exceptions/{exception_id}/analysis",
    response_model=AnalysisResponse,
    dependencies=[Depends(require_role("analyst", "manager", "admin"))],
)
async def get_analysis(
    exception_id: str,
    tenant_id: str = Depends(get_tenant_id),
) -> AnalysisResponse:
    record = _get_or_404(exception_id, tenant_id)

    # Try to extract analysis from trace data
    trace_data = exception_store.get_trace(exception_id)

    # Build analysis from trace_data if available, else from record fields
    diagnosis = "No analysis available"
    confidence = 0
    risk = "unknown"
    resolution = "pending"
    lines: list[LineAnalysis] = []

    if trace_data:
        diagnosis = trace_data.get("explanation") or diagnosis
        risk = "low" if trace_data.get("shadow_verdict") == "GREEN" else (
            "medium" if trace_data.get("shadow_verdict") == "YELLOW" else "high"
        )
        confidence = 80 if trace_data.get("intent_selected") else 0
        resolution = trace_data.get("final_status") or resolution

        # Extract per-line analysis if present
        raw_lines = trace_data.get("line_analysis", [])
        for la in raw_lines:
            lines.append(
                LineAnalysis(
                    line_id=la.get("line_id", ""),
                    diagnosis=la.get("diagnosis", ""),
                    resolution=la.get("resolution", ""),
                    risk=la.get("risk", "unknown"),
                    waterfall=la.get("waterfall", []),
                )
            )
    else:
        # Construct basic response from record fields
        if record.intent:
            diagnosis = f"Intent classified as {record.intent}"
            confidence = 70
        if record.shadow_verdict:
            risk = "low" if record.shadow_verdict == "GREEN" else (
                "medium" if record.shadow_verdict == "YELLOW" else "high"
            )
        if record.final_status:
            resolution = record.final_status

    return AnalysisResponse(
        diagnosis=diagnosis,
        confidence=confidence,
        risk=risk,
        resolution=resolution,
        lines=lines,
    )
