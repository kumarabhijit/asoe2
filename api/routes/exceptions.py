"""Exception management endpoints (architecture_v3.md Section 8.2).

POST  /api/v1/exceptions/resolve         — synchronous resolution
POST  /api/v1/exceptions/resolve/async   — async resolution (stub)
POST  /api/v1/exceptions/resolve/explain — explain mode dry-run
GET   /api/v1/exceptions                 — paginated exception queue
GET   /api/v1/exceptions/stats           — dashboard metrics
GET   /api/v1/exceptions/{id}            — exception detail
GET   /api/v1/exceptions/{id}/trace      — full TraceRecord
PATCH /api/v1/exceptions/{id}/override   — override agent recommendation across
                                          GREEN/YELLOW/RED lifecycles (manager+)
POST  /api/v1/exceptions/{id}/escalate   — route exception to ESCALATED (analyst+)
POST  /api/v1/exceptions/{id}/approve    — resume paused exception (manager+)
POST  /api/v1/exceptions/{id}/reject     — reject paused exception (manager+)
POST  /api/v1/exceptions/{id}/challenge  — GREEN: post-execution challenge (analyst+)
POST  /api/v1/exceptions/{id}/admin-release — RED: admin release of blocked exception (admin)

Unified action model (Phase 1 — Option A):
  Override spans GREEN (RESOLVED), YELLOW (PENDING_REVIEW/ESCALATED), and
  RED (BLOCKED) verdicts for privileged users (manager+). Analysts cannot
  Override; they use Approve/Reject on YELLOW and Escalate for routing.
  Escalate is a separate routing event with its own endpoint, permission
  (exceptions:escalate, granted analyst+), and audit event
  (EXCEPTION_ESCALATED).

Security:
  §11.3 — Partner-role users see only their own orders (retailer_id filtering)
  §11.4 — X-Trace-ID propagated from request header into graph execution
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Query, Request

from api.deps import (
    AuthenticatedUser,
    get_current_user,
    get_tenant_id,
    require_permission,
    require_role,
)
from api.analysis_adapters import (
    ANALYSIS_ADAPTERS,
    SECONDARY_ANALYSIS_ADAPTERS,
    resolve_adapter_key,
)
from api.analysis_composer import compose as compose_analysis
from api.errors import ASOEError
from api.schemas import (
    AdminReleaseRequest,
    AnalysisResponse,
    AsyncResolveResponse,
    ChallengeRequest,
    CosignRequest,
    DispositionRequest,
    EscalateRequest,
    ExceptionDetailResponse,
    ExceptionListResponse,
    LineAnalysis,
    LineItem,
    LineItemsResponse,
    ReanalyzeRequest,
    ResolveRequest,
    ResolveResponse,
    StatsResponse,
    TraceResponse,
)
from contracts.policy import (
    HIGH_VALUE_OVERRIDE_THRESHOLD_USD,
    REANALYSIS_MAX_ATTEMPTS,
)
from api.events import WSEvent
from api.pubsub import event_publisher
from api.store import exception_store
from constraints.specs import (
    INTENT_REASON_TAGS,
    AllowedOverrideReasonTag,
    AllowedResolutionAction,
)
from contracts.models import (
    ADMIN_RELEASE_SOURCE_STATES,
    CHALLENGE_SOURCE_STATES,
    COSIGN_ELIGIBLE_STATES,
    ESCALATE_ELIGIBLE_STATES,
    HITL_DISPOSITION_STATES,
    HITL_OVERRIDE_STATES,
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
    # Verdict Pillar 1: persist gateway context alongside recipe output.
    # `state.enrichment_context` is the sole source — `resolve_dependencies`
    # writes gateway results there directly. Copy so later mutation of
    # the graph state doesn't leak into the persisted record.
    ctx = dict(state.enrichment_context)

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
        enrichment_context=ctx,
    )

    # Verdict Pillar 2.3: capture a structured audit-gap snapshot on
    # the trace. The build_analysis node already flipped final_status
    # when coverage was incomplete; this block surfaces WHICH fields
    # were missing so auditors see a machine-readable breakdown
    # alongside the prose explanation.
    from api.analysis_composer import compose as _compose_record
    _composed = _compose_record(record)
    audit_missing_class: Optional[str] = None
    audit_missing_fields: List[str] = []
    if _composed.class_name and _composed.missing_audit_fields:
        audit_missing_class = _composed.class_name
        audit_missing_fields = list(_composed.missing_audit_fields)

    trace_data = {
        "trace_id": trace_id or "",
        "event_id": state.event.order_id,
        "skill_name": state.skill.name if state.skill else None,
        "intent_selected": state.intent.value if state.intent else None,
        # Persist the real classifier confidence (0.0-1.0 float). This is
        # the value the IntentClassifier's backend produced — fallback
        # backends produce 0.80–0.95 by intent; remote LLMs produce
        # whatever the model returned. Without persisting it here the
        # /analysis read path had no source for AnalysisResponse.confidence
        # and resorted to a hardcoded 80, making every UI pill display
        # 80% regardless of the LLM's actual output.
        "intent_confidence": state.confidence,
        "shadow_verdict": state.shadow.status.value if state.shadow else None,
        "shadow_policy_hits": state.shadow.policy_hits if state.shadow else [],
        "recipe_name": state.selected_recipe,
        "constrained_output_schemas": state.execution_log.constrained_outputs if state.execution_log else {},
        "gateway_calls": [],
        "backend_fallback": "deterministic_fallback",
        "is_fallback_generated": True,
        "final_status": state.final_status.value if state.final_status else None,
        "explanation": state.explanation,
        "audit_context_missing_class": audit_missing_class,
        "audit_context_missing_fields": audit_missing_fields,
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
# Idempotency-Key support (Phase 1: in-memory TTL cache)
# ---------------------------------------------------------------------------
#
# Keyed on (tenant_id, exception_id, user_sub, key). A repeat call within
# TTL returns the cached response verbatim; a differing body with the same
# key returns 409 IDEMPOTENCY_CONFLICT.
#
# Phase 1 only — production will move this to Redis. The in-memory map is
# per-process, so it does NOT span replicas. This is acceptable for the
# single-process deployment used today; it is documented so the Phase 2
# migration to Redis is a drop-in replacement.

_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60  # 24h

# Value: (created_at_epoch, request_body_json, cached_response_dict)
_idempotency_cache: Dict[
    Tuple[str, str, str, str],
    Tuple[float, Dict[str, Any], Dict[str, Any]],
] = {}
_idempotency_lock = threading.Lock()


def _validate_idempotency_key(key: Optional[str]) -> Optional[str]:
    """Return the validated key or None; raise 422 on malformed input."""
    if key is None:
        return None
    if not _IDEMPOTENCY_KEY_PATTERN.match(key):
        raise ASOEError(
            code="INVALID_IDEMPOTENCY_KEY",
            message=(
                "Idempotency-Key must be 1-128 chars of [A-Za-z0-9_-]."
            ),
            status_code=422,
        )
    return key


def _idempotency_lookup(
    tenant_id: str,
    exception_id: str,
    user_sub: str,
    key: str,
    request_body: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Return a cached response dict if a valid entry exists.

    Raises 409 IDEMPOTENCY_CONFLICT when the same key is presented with a
    different body. Evicts expired entries on read.
    """
    cache_key = (tenant_id, exception_id, user_sub, key)
    now = time.time()
    with _idempotency_lock:
        entry = _idempotency_cache.get(cache_key)
        if entry is None:
            return None
        created_at, stored_body, cached_response = entry
        if now - created_at > _IDEMPOTENCY_TTL_SECONDS:
            _idempotency_cache.pop(cache_key, None)
            return None
    if stored_body != request_body:
        raise ASOEError(
            code="IDEMPOTENCY_CONFLICT",
            message=(
                "Idempotency-Key reused with a different request body."
            ),
            status_code=409,
        )
    return cached_response


def _idempotency_store(
    tenant_id: str,
    exception_id: str,
    user_sub: str,
    key: str,
    request_body: Dict[str, Any],
    response_body: Dict[str, Any],
) -> None:
    cache_key = (tenant_id, exception_id, user_sub, key)
    with _idempotency_lock:
        _idempotency_cache[cache_key] = (time.time(), request_body, response_body)


def _clear_idempotency_cache() -> None:
    """Test helper: wipe the module-level cache between tests."""
    with _idempotency_lock:
        _idempotency_cache.clear()


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp (second precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _financial_impact_usd(record) -> Optional[float]:
    """Extract the record's financial impact for four-eyes gating.

    Different recipes surface the impact under different keys; keep the
    lookup lenient. Absent or non-numeric values are treated as "not
    available" (None) — the four-eyes gate then only fires when the
    impact is explicit, avoiding false cosign blocks on records where we
    simply can't measure materiality.
    """
    data = record.resolution_data or {}
    for key in ("financial_impact_usd", "financial_impact", "impact_usd"):
        val = data.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    return None


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
# POST /api/v1/exceptions/{id}/override/cosign — Four-eyes second reviewer
# ---------------------------------------------------------------------------

@router.post(
    "/exceptions/{exception_id}/override/cosign",
    response_model=ExceptionDetailResponse,
    dependencies=[Depends(require_role("manager", "admin"))],
)
async def cosign_override(
    exception_id: str,
    req: CosignRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ExceptionDetailResponse:
    """Second-reviewer decision on a pending high-value override.

    Gating:
      - Lifecycle must be PENDING_COSIGN.
      - Caller must not be the pending_override.initiator (SoD).
      - Idempotency-Key supported identically to /override.

    Outcome:
      - approve=True  → apply the pending override (lifecycle → RESOLVED,
                        resolved_by = initiator, cosigned_by = caller,
                        EXCEPTION_OVERRIDE_COSIGNED audit event).
      - approve=False → restore the prior lifecycle_state (stashed on the
                        pending_override), clear pending_override,
                        EXCEPTION_OVERRIDE_REJECTED audit event.
    """
    record = _get_or_404(exception_id, tenant_id)

    # Idempotency short-circuit — same rationale as /override: a retry must
    # return the cached answer rather than fail state-transition guards.
    key = _validate_idempotency_key(idempotency_key)
    request_body = req.model_dump()
    if key is not None:
        cached = _idempotency_lookup(
            tenant_id, exception_id, user.sub, key, request_body,
        )
        if cached is not None:
            return ExceptionDetailResponse.model_validate(cached)

    _require_state(record, COSIGN_ELIGIBLE_STATES, "override/cosign")

    pending = (record.resolution_data or {}).get("pending_override")
    if not pending or "initiator" not in pending:
        raise ASOEError(
            code="NO_PENDING_OVERRIDE",
            message="Record is in PENDING_COSIGN but carries no pending_override metadata.",
            status_code=409,
        )

    if pending["initiator"] == user.sub:
        raise ASOEError(
            code="SOD_VIOLATION",
            message=(
                "Segregation of duties: the user who initiated the override "
                "cannot cosign their own action. A different authorized user "
                "must cosign."
            ),
            status_code=403,
        )

    if not req.notes or not req.notes.strip():
        raise ASOEError(
            code="NOTES_REQUIRED",
            message="Cosign notes are mandatory (SOX audit requirement).",
            status_code=422,
        )

    prior_snapshot = {
        "lifecycle_state": record.lifecycle_state,
        "pending_action": pending.get("action"),
        "pending_reason_tag": pending.get("reason_tag"),
        "initiator": pending["initiator"],
        "exception_id": exception_id,
    }

    if req.approve:
        # Apply the pending override. resolved_by is set to the INITIATOR
        # (they proposed the action); cosigned_by and cosigned_at capture
        # the reviewer for the audit trail.
        merged_resolution_data = dict(record.resolution_data or {})
        merged_resolution_data.pop("pending_override", None)
        merged_resolution_data["cosign"] = {
            "cosigned_by": user.sub,
            "cosigned_at": _utc_now_iso(),
            "cosign_notes": req.notes,
            "initiator": pending["initiator"],
            "initiated_at": pending.get("initiated_at"),
        }
        updated = exception_store.update(
            exception_id,
            tenant_id,
            resolved_by=pending["initiator"],
            resolved_action=pending["action"],
            resolution_notes=pending.get("notes"),
            lifecycle_state="RESOLVED",
            final_status="COMPLETE",
            resolution_data=merged_resolution_data,
        )
        if not updated:
            raise ASOEError(code="UPDATE_FAILED", message="Failed to apply override.", status_code=500)
        exception_store.log_audit_event(
            tenant_id=tenant_id,
            policy_key="EXCEPTION_OVERRIDE_COSIGNED",
            previous_value=prior_snapshot,
            new_value={
                "lifecycle_state": "RESOLVED",
                "resolved_action": pending["action"],
                "reason_tag": pending.get("reason_tag"),
                "initiator": pending["initiator"],
                "cosigned_by": user.sub,
            },
            changed_by=user.sub,
            change_reason=req.notes,
        )
    else:
        # Restore prior lifecycle; clear pending_override; audit rejection.
        from_lifecycle = pending.get("from_lifecycle_state") or "PENDING_REVIEW"
        merged_resolution_data = dict(record.resolution_data or {})
        merged_resolution_data.pop("pending_override", None)
        merged_resolution_data["cosign_rejection"] = {
            "rejected_by": user.sub,
            "rejected_at": _utc_now_iso(),
            "rejection_notes": req.notes,
            "initiator": pending["initiator"],
        }
        updated = exception_store.update(
            exception_id,
            tenant_id,
            lifecycle_state=from_lifecycle,
            resolution_data=merged_resolution_data,
        )
        if not updated:
            raise ASOEError(code="UPDATE_FAILED", message="Failed to reject override.", status_code=500)
        exception_store.log_audit_event(
            tenant_id=tenant_id,
            policy_key="EXCEPTION_OVERRIDE_REJECTED",
            previous_value=prior_snapshot,
            new_value={
                "lifecycle_state": from_lifecycle,
                "initiator": pending["initiator"],
                "rejected_by": user.sub,
            },
            changed_by=user.sub,
            change_reason=req.notes,
        )

    response = updated.to_detail()
    if key is not None:
        _idempotency_store(
            tenant_id, exception_id, user.sub, key,
            request_body, response.model_dump(),
        )
    return response


# ---------------------------------------------------------------------------
# PATCH /api/v1/exceptions/{id}/disposition — v2 unified HITL primitive
# ---------------------------------------------------------------------------
#
# Collapses Approve / Reject / Override into a single endpoint that emits
# one EXCEPTION_RESOLVED audit event with a sub_type discriminator. Existing
# /approve, /reject, /override endpoints remain as-is for backward compat;
# Phase 3 will migrate call sites and retire them.
#
# Sub-type derivation (computed server-side; clients do not set it):
#   chosen_action == NO_ACTION           → REJECT
#   chosen_action == recommended_action  → APPROVE
#   chosen_action != recommended_action  → OVERRIDE (four-eyes aware)
#
# Permission gating:
#   APPROVE / REJECT  → exceptions:approve
#   OVERRIDE          → exceptions:override (delegates to the override
#                       handler's gate — verdict, SoD, four-eyes).


def _recommended_action_of(record) -> Optional[str]:
    val = (record.resolution_data or {}).get("recommended_action")
    return val if isinstance(val, str) else None


@router.patch(
    "/exceptions/{exception_id}/disposition",
    response_model=ExceptionDetailResponse,
    dependencies=[Depends(require_permission("exceptions:approve"))],
)
async def disposition_exception(
    exception_id: str,
    req: DispositionRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ExceptionDetailResponse:
    """Unified HITL disposition: single endpoint for Approve/Reject/Override.

    See module comment above for sub-type rules and permission gating.
    Emits EXCEPTION_RESOLVED with sub_type=APPROVE|REJECT|OVERRIDE so
    compliance tooling can answer "how often do managers deviate from
    the agent?" with one SQL query across a consistent event stream.
    """
    record = _get_or_404(exception_id, tenant_id)

    # Idempotency short-circuit — same rationale as /override.
    key = _validate_idempotency_key(idempotency_key)
    request_body = req.model_dump()
    if key is not None:
        cached = _idempotency_lookup(
            tenant_id, exception_id, user.sub, key, request_body,
        )
        if cached is not None:
            return ExceptionDetailResponse.model_validate(cached)

    # Validate action + reason_tag against constrained vocabularies up front.
    allowed_actions = list(AllowedResolutionAction.__args__)  # type: ignore[attr-defined]
    # NO_ACTION is a valid disposition signal even though it isn't in the
    # AllowedResolutionAction recipe vocabulary — it means "resolve by
    # declining to act." Accept it alongside the recipe set.
    if req.action != "NO_ACTION" and req.action not in allowed_actions:
        raise ASOEError(
            code="INVALID_ACTION",
            message=(
                f"Action '{req.action}' is not allowed. "
                f"Valid: {allowed_actions + ['NO_ACTION']}"
            ),
            status_code=422,
        )
    # Per-intent reason-tag validation (Phase 3 Option A framework).
    # When the record carries a known intent, narrow the allowed set to
    # INTENT_REASON_TAGS[intent]; otherwise fall back to the global
    # AllowedOverrideReasonTag set (FAILED-lifecycle records and any
    # intent not yet in the map). Today all per-intent sets equal the
    # global set, so this is a no-op in behavior — the framework is
    # ready for a data-only swap when product/compliance curates real
    # per-intent categories.
    global_tags = list(AllowedOverrideReasonTag.__args__)  # type: ignore[attr-defined]
    per_intent = INTENT_REASON_TAGS.get(record.intent) if record.intent else None
    allowed_tags = list(per_intent) if per_intent else global_tags
    if req.reason_tag not in allowed_tags:
        raise ASOEError(
            code="INVALID_REASON_TAG",
            message=(
                f"reason_tag '{req.reason_tag}' is not allowed"
                + (f" for intent '{record.intent}'" if per_intent else "")
                + f". Valid: {allowed_tags}"
            ),
            status_code=422,
        )
    if not req.notes or not req.notes.strip():
        raise ASOEError(
            code="NOTES_REQUIRED",
            message="Notes are required (SOX audit trail).",
            status_code=422,
        )

    recommended = _recommended_action_of(record)
    if req.action == "NO_ACTION":
        sub_type = "REJECT"
        new_lifecycle = "REJECTED"
    elif recommended is not None and req.action == recommended:
        sub_type = "APPROVE"
        new_lifecycle = "RESOLVED"
    else:
        sub_type = "OVERRIDE"
        new_lifecycle = "RESOLVED"

    # OVERRIDE sub-type requires the override permission AND triggers the
    # same guards as /override (verdict, state, SoD, four-eyes).
    if sub_type == "OVERRIDE":
        if "exceptions:override" not in user.permissions:
            raise ASOEError(
                code="PERMISSION_DENIED",
                message=(
                    "This disposition differs from the recommended action "
                    "and requires the exceptions:override permission."
                ),
                status_code=403,
            )
        if record.shadow_verdict not in {"GREEN", "YELLOW", "RED"}:
            raise ASOEError(
                code="INVALID_VERDICT",
                message=(
                    f"Cannot override: shadow_verdict='{record.shadow_verdict}' "
                    f"has no overridable agent decision."
                ),
                status_code=409,
            )
        _require_state(record, HITL_OVERRIDE_STATES, "override")
        prior_resolver = record.resolved_by
        if (
            prior_resolver
            and not prior_resolver.startswith("system:")
            and prior_resolver == user.sub
        ):
            raise ASOEError(
                code="SOD_VIOLATION",
                message=(
                    "Segregation of duties: the user who previously resolved "
                    "this exception cannot override it."
                ),
                status_code=403,
            )
        # Four-eyes: high-value overrides stage to PENDING_COSIGN.
        impact = _financial_impact_usd(record)
        if impact is not None and impact >= HIGH_VALUE_OVERRIDE_THRESHOLD_USD:
            prior_snapshot = {
                "lifecycle_state": record.lifecycle_state,
                "shadow_verdict": record.shadow_verdict,
                "recommended_action": recommended,
                "exception_id": exception_id,
            }
            pending_override = {
                "action": req.action,
                "notes": req.notes,
                "reason_tag": req.reason_tag,
                "initiator": user.sub,
                "initiated_at": _utc_now_iso(),
                "financial_impact_usd": impact,
                "from_lifecycle_state": record.lifecycle_state,
            }
            merged = dict(record.resolution_data or {})
            merged["pending_override"] = pending_override
            updated = exception_store.update(
                exception_id,
                tenant_id,
                lifecycle_state="PENDING_COSIGN",
                resolution_data=merged,
            )
            if not updated:
                raise ASOEError(
                    code="UPDATE_FAILED",
                    message="Failed to stage pending override.",
                    status_code=500,
                )
            exception_store.log_audit_event(
                tenant_id=tenant_id,
                policy_key="EXCEPTION_OVERRIDE_INITIATED",
                previous_value=prior_snapshot,
                new_value={
                    "lifecycle_state": "PENDING_COSIGN",
                    "pending_action": req.action,
                    "reason_tag": req.reason_tag,
                    "financial_impact_usd": impact,
                    "initiated_via": "disposition",
                },
                changed_by=user.sub,
                change_reason=req.notes,
            )
            response = updated.to_detail()
            if key is not None:
                _idempotency_store(
                    tenant_id, exception_id, user.sub, key,
                    request_body, response.model_dump(),
                )
            return response
    else:
        # APPROVE / REJECT also require the record to be in a state where
        # a disposition is meaningful. Reuse the existing HITL sets.
        _require_state(record, HITL_DISPOSITION_STATES, sub_type.lower())

    prior_snapshot = {
        "lifecycle_state": record.lifecycle_state,
        "shadow_verdict": record.shadow_verdict,
        "recommended_action": recommended,
        "exception_id": exception_id,
    }

    updated = exception_store.update(
        exception_id,
        tenant_id,
        resolved_by=user.sub,
        resolved_action=req.action,
        resolution_notes=req.notes,
        lifecycle_state=new_lifecycle,
        final_status="COMPLETE" if new_lifecycle == "RESOLVED" else "REJECTED",
    )
    if not updated:
        raise ASOEError(
            code="UPDATE_FAILED",
            message="Failed to update exception.",
            status_code=500,
        )

    exception_store.log_audit_event(
        tenant_id=tenant_id,
        policy_key="EXCEPTION_RESOLVED",
        previous_value=prior_snapshot,
        new_value={
            "sub_type": sub_type,
            "resolved_action": req.action,
            "lifecycle_state": new_lifecycle,
            "reason_tag": req.reason_tag,
            "initiated_via": "disposition",
        },
        changed_by=user.sub,
        change_reason=req.notes,
    )

    response = updated.to_detail()
    if key is not None:
        _idempotency_store(
            tenant_id, exception_id, user.sub, key,
            request_body, response.model_dump(),
        )
    return response


# ---------------------------------------------------------------------------
# POST /api/v1/exceptions/{id}/escalate — Dedicated escalate routing
# ---------------------------------------------------------------------------

@router.post(
    "/exceptions/{exception_id}/escalate",
    response_model=ExceptionDetailResponse,
    dependencies=[Depends(require_permission("exceptions:escalate"))],
)
async def escalate_exception(
    exception_id: str,
    req: EscalateRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ExceptionDetailResponse:
    """Escalate an exception into ESCALATED lifecycle (routing only).

    Decoupled from Override: this endpoint changes routing without
    asserting a resolution action. Eligible lifecycles are
    ESCALATE_ELIGIBLE_STATES (PENDING_REVIEW, FAILED, BLOCKED). Already
    ESCALATED (no-op) and RESOLVED (use Challenge) return 409.

    Supports the optional Idempotency-Key header (same semantics as
    /override).
    """
    record = _get_or_404(exception_id, tenant_id)
    _require_state(record, ESCALATE_ELIGIBLE_STATES, "escalate")

    key = _validate_idempotency_key(idempotency_key)
    request_body = req.model_dump()
    if key is not None:
        cached = _idempotency_lookup(
            tenant_id, exception_id, user.sub, key, request_body,
        )
        if cached is not None:
            return ExceptionDetailResponse.model_validate(cached)

    # Snapshot prior lifecycle before mutating the live record.
    prior_lifecycle = record.lifecycle_state

    # Append escalation metadata without overwriting prior resolution_data.
    resolution_data = dict(record.resolution_data or {})
    escalations = list(resolution_data.get("escalations") or [])
    escalations.append({
        "reason": req.reason,
        "to_role": req.to_role,
        "escalated_by": user.sub,
        "escalated_at": datetime.now(timezone.utc).isoformat(),
        "from_lifecycle_state": prior_lifecycle,
    })
    resolution_data["escalations"] = escalations

    updated = exception_store.update(
        exception_id,
        tenant_id,
        lifecycle_state="ESCALATED",
        resolution_data=resolution_data,
    )
    if not updated:
        raise ASOEError(
            code="UPDATE_FAILED",
            message="Failed to update exception.",
            status_code=500,
        )

    exception_store.log_audit_event(
        tenant_id=tenant_id,
        policy_key="EXCEPTION_ESCALATED",
        previous_value={
            "lifecycle_state": prior_lifecycle,
            "exception_id": exception_id,
        },
        new_value={
            "lifecycle_state": "ESCALATED",
            "reason": req.reason,
            "to_role": req.to_role,
        },
        changed_by=user.sub,
        change_reason=req.reason,
    )

    response = updated.to_detail()
    if key is not None:
        _idempotency_store(
            tenant_id, exception_id, user.sub, key,
            request_body, response.model_dump(),
        )
    return response


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

    # Announce the reanalysis before run_graph so subscribed UIs can flip
    # the detail panel into a "re-running" state and render the streaming
    # pipeline_progress events that follow.
    event_publisher.publish(
        tenant_id,
        WSEvent.reanalysis_started(
            trace_id=trace_id,
            exception_id=exception_id,
            tenant_id=tenant_id,
            attempt=attempts_so_far + 1,
            triggered_by=user.sub,
            reason=req.reason,
            prior_trace_id=record.trace_id,
            prior_shadow_verdict=record.shadow_verdict,
            prior_final_status=record.final_status,
        ),
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
        # Persist the real classifier confidence on the reanalysis path
        # too — same rationale as the /resolve write site.
        "intent_confidence": final_state.confidence,
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
        # Read the real classifier confidence persisted alongside
        # intent_selected (see _store_trace at line ~217). The value is
        # stored as a 0.0-1.0 float (IntentDecision.confidence); the
        # AnalysisResponse contract is 0-100 int, so scale + clamp here.
        # A missing/null confidence falls back to 0 — never a fabricated
        # mid-range default. Verdict 2026-04-22 / Guardrail #6: do not
        # surface synthetic values that the operator can't distinguish
        # from real LLM output.
        raw_conf = trace_data.get("intent_confidence")
        if isinstance(raw_conf, (int, float)) and raw_conf > 0:
            confidence = max(0, min(100, int(round(raw_conf * 100))))
        else:
            confidence = 0
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
            # No trace data → no real confidence available. Stay at 0
            # rather than fabricating 70; the UI hides the bar when
            # confidence is missing/zero.
            confidence = 0
        if record.shadow_verdict:
            risk = "low" if record.shadow_verdict == "GREEN" else (
                "medium" if record.shadow_verdict == "YELLOW" else "high"
            )
        if record.final_status:
            resolution = record.final_status

    # Verdict Pillar 2.2: route the read path through the composer so
    # registry coverage is honoured on reads AND emissions. The
    # composer returns a ComposedAnalysis with (a) the typed
    # projection, (b) the missing-audit-field list, (c) the class
    # name in play. At graph-execution time the build_analysis node
    # already flipped records with incomplete coverage to
    # AUDIT_CONTEXT_MISSING — but if an older record (created
    # before Pillar 2 shipped) or a grandfathered field is in play,
    # coverage here can still be incomplete. When that happens we
    # emit the projection (so the UI gets what it can) but suppress
    # it under `_audit_coverage: "incomplete"` so downstream
    # consumers know the surface is partial.
    composed = compose_analysis(record)
    extras: Dict[str, Any] = {}
    if composed.class_name and composed.projection is not None:
        # Find the AnalysisResponse field this projection lands on.
        # Adapter registry's second element is the target field name
        # — look it up by resolving the adapter key for this record.
        adapter_key = resolve_adapter_key(record)
        if adapter_key and adapter_key in ANALYSIS_ADAPTERS:
            field_name, _adapter = ANALYSIS_ADAPTERS[adapter_key]
            # Per Dana's mandate: never ship partial-truth. If the
            # composer flagged missing audit-bearing fields, do NOT
            # surface the projection as if it were authoritative.
            # Grandfathered missing fields don't block (the clause
            # handles that at the classifier level).
            if composed.is_complete:
                extras[field_name] = composed.projection
                # Secondary projections share the primary's attestation
                # target (registry rationale "same attestation target").
                # Only surface when the primary cleared audit coverage
                # — they ride on its enforcement.
                for sec_field, sec_adapter in SECONDARY_ANALYSIS_ADAPTERS.get(
                    adapter_key, ()
                ):
                    sec_proj = sec_adapter(record)
                    if sec_proj is not None:
                        extras[sec_field] = sec_proj

    return AnalysisResponse(
        diagnosis=diagnosis,
        confidence=confidence,
        risk=risk,
        resolution=resolution,
        lines=lines,
        **extras,
    )
