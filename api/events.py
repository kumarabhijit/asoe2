"""Real-time event schemas (architecture_v3.md Section 10.2).

Typed Pydantic models for events published to Redis Pub/Sub and
forwarded to WebSocket clients. All events share a common envelope
and carry type-specific payloads.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


EventType = Literal[
    "pipeline_progress", "exception_update", "task_complete", "error",
    "reanalysis_started",
    # ADR-038 §H.6 / Phase 28.5 — case-level events. Emitted on
    # OrderCase open / status change / SLA breach / close so that
    # `/inbox`, `/exceptions`, and `/cases` (all projected from the
    # same case set in V5.1) can invalidate their list views without
    # polling. Carry `case_id` rather than `exception_id`; the WS
    # envelope makes both fields optional so the same channel can
    # multiplex per-event and per-case streams.
    "case_open", "case_update", "case_close",
]
NodeStatus = Literal["started", "completed", "failed"]


class PipelineProgressPayload(BaseModel):
    """Pipeline-progress event payload.

    ADR-027 Phase B emits this once per record state change, carrying
    the full executed_nodes list to date in `executed_nodes`. The
    legacy per-node fields (`node`, `status`, `duration_ms`, `data`)
    remain for back-compat with the unbatched per-node emission shape
    declared at Phase 4 — they are unused on the Phase B path.
    The UI is idempotent on full-payload refresh, so reconciling
    against `executed_nodes` is safe.

    Per ADR-027 rev. 2 §"WebSocket batching": batching avoids ~11x
    event-volume amplification on the WS path (Container Apps closes
    idle WS at 4 min; see asoe-ui ec69bb5 polling fallback).
    """
    node: Optional[str] = None
    status: Optional[NodeStatus] = None
    duration_ms: Optional[int] = None
    data: Optional[Dict[str, Any]] = None
    executed_nodes: List[Dict[str, Any]] = Field(default_factory=list)
    final_status: Optional[str] = None


class ExceptionUpdatePayload(BaseModel):
    """Published on exception lifecycle state transitions."""
    lifecycle_state: str
    updated_fields: List[str] = Field(default_factory=list)


class TaskCompletePayload(BaseModel):
    """Published when an async task finishes."""
    task_id: str
    final_status: str
    explanation: Optional[str] = None


class ErrorPayload(BaseModel):
    """Published on pipeline errors."""
    code: str
    message: str


class ReanalysisStartedPayload(BaseModel):
    """Published at the start of a human-triggered reanalysis, before
    run_graph() runs. Subscribed clients can switch the detail panel into
    a re-running state while the real pipeline events stream in."""
    attempt: int
    triggered_by: str
    reason: str
    prior_trace_id: Optional[str] = None
    prior_shadow_verdict: Optional[str] = None
    prior_final_status: Optional[str] = None


# ADR-038 §H.6 / Phase 28.5 — case-level event payloads. Mirrors the
# UI's `WSEvent.case_*` invalidation contract (`useCases` hook).


class CaseOpenedPayload(BaseModel):
    """Published when a new OrderCase is materialised (T2/T3)."""
    source: str
    source_channel: str
    status: str
    sla_deadline: Optional[str] = None
    customer_po_number: Optional[str] = None
    sales_order_id: Optional[str] = None


class CaseUpdatedPayload(BaseModel):
    """Published on case status / SLA / working-memory changes.

    `updated_fields` enumerates which OrderCase fields changed so the
    UI can decide whether a re-fetch is needed (status change → yes,
    working_memory_summary touch → maybe). `updated_at` is the new
    canonical "last activity" timestamp the UI surfaces (issue #133
    PO #17); always present from the V014 migration onward.
    """
    status: str
    updated_fields: List[str] = Field(default_factory=list)
    sla_deadline: Optional[str] = None
    updated_at: Optional[str] = None


class CaseClosedPayload(BaseModel):
    """Published when a case reaches RESOLVED / FAILED / BLOCKED."""
    status: str
    closed_at: str


class WSEvent(BaseModel):
    """Standard event envelope for Redis Pub/Sub and WebSocket delivery.

    All events follow this shape. The ``payload`` field carries the
    type-specific data (one of the payload models above, serialized
    as a dict).
    """
    type: EventType
    trace_id: str
    # `exception_id` carries the per-event subject for pipeline /
    # exception / task events. Case events (`case_open`, `case_update`,
    # `case_close`) carry `case_id` instead. Exactly one of the two
    # MUST be set per event type — enforced by the factory methods.
    exception_id: Optional[str] = None
    case_id: Optional[str] = None
    tenant_id: str
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    payload: Dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def pipeline_progress(
        cls,
        trace_id: str,
        exception_id: str,
        tenant_id: str,
        node: Optional[str] = None,
        status: Optional[NodeStatus] = None,
        duration_ms: Optional[int] = None,
        data: Optional[Dict[str, Any]] = None,
        executed_nodes: Optional[List[Dict[str, Any]]] = None,
        final_status: Optional[str] = None,
    ) -> "WSEvent":
        """ADR-027 Phase B: prefer the batched form (executed_nodes +
        final_status). The per-node form (node + status) is preserved
        for back-compat but is not emitted by the orchestrator on the
        Phase B path."""
        payload = PipelineProgressPayload(
            node=node,
            status=status,
            duration_ms=duration_ms,
            data=data,
            executed_nodes=list(executed_nodes or []),
            final_status=final_status,
        )
        return cls(
            type="pipeline_progress",
            trace_id=trace_id,
            exception_id=exception_id,
            tenant_id=tenant_id,
            payload=payload.model_dump(exclude_none=True),
        )

    @classmethod
    def exception_update(
        cls,
        trace_id: str,
        exception_id: str,
        tenant_id: str,
        lifecycle_state: str,
        updated_fields: Optional[List[str]] = None,
    ) -> "WSEvent":
        payload = ExceptionUpdatePayload(
            lifecycle_state=lifecycle_state,
            updated_fields=updated_fields or [],
        )
        return cls(
            type="exception_update",
            trace_id=trace_id,
            exception_id=exception_id,
            tenant_id=tenant_id,
            payload=payload.model_dump(),
        )

    @classmethod
    def task_complete(
        cls,
        trace_id: str,
        exception_id: str,
        tenant_id: str,
        task_id: str,
        final_status: str,
        explanation: Optional[str] = None,
    ) -> "WSEvent":
        payload = TaskCompletePayload(
            task_id=task_id, final_status=final_status, explanation=explanation,
        )
        return cls(
            type="task_complete",
            trace_id=trace_id,
            exception_id=exception_id,
            tenant_id=tenant_id,
            payload=payload.model_dump(exclude_none=True),
        )

    @classmethod
    def error(
        cls,
        trace_id: str,
        exception_id: str,
        tenant_id: str,
        code: str,
        message: str,
    ) -> "WSEvent":
        payload = ErrorPayload(code=code, message=message)
        return cls(
            type="error",
            trace_id=trace_id,
            exception_id=exception_id,
            tenant_id=tenant_id,
            payload=payload.model_dump(),
        )

    @classmethod
    def reanalysis_started(
        cls,
        trace_id: str,
        exception_id: str,
        tenant_id: str,
        attempt: int,
        triggered_by: str,
        reason: str,
        prior_trace_id: Optional[str] = None,
        prior_shadow_verdict: Optional[str] = None,
        prior_final_status: Optional[str] = None,
    ) -> "WSEvent":
        payload = ReanalysisStartedPayload(
            attempt=attempt,
            triggered_by=triggered_by,
            reason=reason,
            prior_trace_id=prior_trace_id,
            prior_shadow_verdict=prior_shadow_verdict,
            prior_final_status=prior_final_status,
        )
        return cls(
            type="reanalysis_started",
            trace_id=trace_id,
            exception_id=exception_id,
            tenant_id=tenant_id,
            payload=payload.model_dump(exclude_none=True),
        )

    # ------------------------------------------------------------------
    # Case-level events (ADR-038 §H.6 / Phase 28.5)
    # ------------------------------------------------------------------

    @classmethod
    def case_open(
        cls,
        trace_id: str,
        case_id: str,
        tenant_id: str,
        source: str,
        source_channel: str,
        status: str,
        sla_deadline: Optional[str] = None,
        customer_po_number: Optional[str] = None,
        sales_order_id: Optional[str] = None,
    ) -> "WSEvent":
        payload = CaseOpenedPayload(
            source=source,
            source_channel=source_channel,
            status=status,
            sla_deadline=sla_deadline,
            customer_po_number=customer_po_number,
            sales_order_id=sales_order_id,
        )
        return cls(
            type="case_open",
            trace_id=trace_id,
            case_id=case_id,
            tenant_id=tenant_id,
            payload=payload.model_dump(exclude_none=True),
        )

    @classmethod
    def case_update(
        cls,
        trace_id: str,
        case_id: str,
        tenant_id: str,
        status: str,
        updated_fields: Optional[List[str]] = None,
        sla_deadline: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> "WSEvent":
        payload = CaseUpdatedPayload(
            status=status,
            updated_fields=updated_fields or [],
            sla_deadline=sla_deadline,
            updated_at=updated_at,
        )
        return cls(
            type="case_update",
            trace_id=trace_id,
            case_id=case_id,
            tenant_id=tenant_id,
            payload=payload.model_dump(exclude_none=True),
        )

    @classmethod
    def case_close(
        cls,
        trace_id: str,
        case_id: str,
        tenant_id: str,
        status: str,
        closed_at: str,
    ) -> "WSEvent":
        payload = CaseClosedPayload(status=status, closed_at=closed_at)
        return cls(
            type="case_close",
            trace_id=trace_id,
            case_id=case_id,
            tenant_id=tenant_id,
            payload=payload.model_dump(),
        )
