"""Exception management endpoints (architecture_v3.md Section 8.2).

POST /api/v1/exceptions/resolve         — synchronous resolution
POST /api/v1/exceptions/resolve/async   — async resolution (stub)
POST /api/v1/exceptions/resolve/explain — explain mode dry-run
GET  /api/v1/exceptions                 — paginated exception queue
GET  /api/v1/exceptions/stats           — dashboard metrics
GET  /api/v1/exceptions/{id}            — exception detail
GET  /api/v1/exceptions/{id}/trace      — full TraceRecord
PATCH /api/v1/exceptions/{id}/override  — human override
POST  /api/v1/exceptions/{id}/approve   — resume paused exception
POST  /api/v1/exceptions/{id}/reject    — reject paused exception
"""

from __future__ import annotations

import logging
import os
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Query

from api.deps import AuthenticatedUser, get_current_user, get_tenant_id, require_role
from api.errors import ASOEError
from api.schemas import (
    ApproveRequest,
    AsyncResolveResponse,
    ExceptionDetailResponse,
    ExceptionListResponse,
    OverrideRequest,
    RejectRequest,
    ResolveRequest,
    ResolveResponse,
    StatsResponse,
    TraceResponse,
)
from api.store import exception_store
from contracts.models import GraphState, OrderEvent

logger = logging.getLogger("asoe.api.exceptions")

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_order_event(req: ResolveRequest) -> OrderEvent:
    """Construct an OrderEvent from the API request body."""
    return OrderEvent(
        order_id=req.order_id,
        line_item=req.line_item,
        sku=req.sku,
        event_type=req.event_type,
        po_price=req.po_price,
        sap_base_price=req.sap_base_price,
        retailer_id=req.retailer_id,
        event_ts=req.event_ts,
        requester_role=req.requester_role,
        credit_limit=req.credit_limit,
        current_exposure=req.current_exposure,
        line_count=req.line_count,
        metadata=req.metadata,
    )


def _run_graph_safe(state: GraphState) -> GraphState:
    """Run the graph with proper error handling."""
    from orchestration.graph import run_graph
    return run_graph(state)


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
    )

    # Store trace data for the /trace endpoint
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


# ---------------------------------------------------------------------------
# POST /api/v1/exceptions/resolve — Synchronous resolution
# ---------------------------------------------------------------------------

@router.post(
    "/exceptions/resolve",
    response_model=ResolveResponse,
    dependencies=[Depends(require_role("analyst", "manager", "admin"))],
)
async def resolve(
    req: ResolveRequest,
    tenant_id: str = Depends(get_tenant_id),
) -> ResolveResponse:
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
        )

    trace_id = final_state.shadow.trace_id if final_state.shadow else None
    exception_id = _persist_exception(tenant_id, final_state, trace_id)
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
    req: ResolveRequest,
    tenant_id: str = Depends(get_tenant_id),
) -> AsyncResolveResponse:
    # V1 stub: runs synchronously, returns as if queued.
    # Real implementation uses Celery/ARQ task queue + Redis.
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
        )

    trace_id = final_state.shadow.trace_id if final_state.shadow else None
    _persist_exception(tenant_id, final_state, trace_id)
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
    req: ResolveRequest,
    tenant_id: str = Depends(get_tenant_id),
) -> ResolveResponse:
    event = _build_order_event(req)
    state = GraphState(event=event)

    # Force explain mode for this request
    prev = os.environ.get("ASOE_EXPLAIN_MODE")
    os.environ["ASOE_EXPLAIN_MODE"] = "1"
    try:
        final_state = _run_graph_safe(state)
    except Exception as exc:
        logger.error("Explain graph execution failed: %s", exc)
        raise ASOEError(
            code="GRAPH_EXECUTION_ERROR",
            message=f"Graph execution failed: {exc}",
            status_code=500,
        )
    finally:
        if prev is None:
            os.environ.pop("ASOE_EXPLAIN_MODE", None)
        else:
            os.environ["ASOE_EXPLAIN_MODE"] = prev

    trace_id = final_state.shadow.trace_id if final_state.shadow else None
    exception_id = _persist_exception(tenant_id, final_state, trace_id)
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
    record = exception_store.get(exception_id, tenant_id)
    if not record:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"Exception {exception_id} not found.",
            status_code=404,
        )
    return record.to_detail()


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
    record = exception_store.get(exception_id, tenant_id)
    if not record:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"Exception {exception_id} not found.",
            status_code=404,
        )

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
) -> ExceptionDetailResponse:
    record = exception_store.get(exception_id, tenant_id)
    if not record:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"Exception {exception_id} not found.",
            status_code=404,
        )

    updated = exception_store.update(
        exception_id,
        tenant_id,
        resolved_by=req.resolved_by,
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
    return updated.to_detail()


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
    record = exception_store.get(exception_id, tenant_id)
    if not record:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"Exception {exception_id} not found.",
            status_code=404,
        )

    if record.lifecycle_state != "PENDING_REVIEW":
        raise ASOEError(
            code="INVALID_STATE",
            message=f"Exception is in state '{record.lifecycle_state}', not PENDING_REVIEW.",
            status_code=409,
        )

    # V1: simple state transition. V1.1 will rehydrate GraphState from
    # PostgresSaver checkpoint and call graph.invoke() to resume.
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
    record = exception_store.get(exception_id, tenant_id)
    if not record:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"Exception {exception_id} not found.",
            status_code=404,
        )

    if record.lifecycle_state != "PENDING_REVIEW":
        raise ASOEError(
            code="INVALID_STATE",
            message=f"Exception is in state '{record.lifecycle_state}', not PENDING_REVIEW.",
            status_code=409,
        )

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
