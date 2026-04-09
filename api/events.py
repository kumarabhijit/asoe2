"""Real-time event schemas (architecture_v3.md Section 10.2).

Typed Pydantic models for events published to Redis Pub/Sub and
forwarded to WebSocket clients. All events share a common envelope
and carry type-specific payloads.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


EventType = Literal["pipeline_progress", "exception_update", "task_complete", "error"]
NodeStatus = Literal["started", "completed", "failed"]


class PipelineProgressPayload(BaseModel):
    """Per-node progress event published as each LangGraph node completes."""
    node: str
    status: NodeStatus
    duration_ms: Optional[int] = None
    data: Optional[Dict[str, Any]] = None


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


class WSEvent(BaseModel):
    """Standard event envelope for Redis Pub/Sub and WebSocket delivery.

    All events follow this shape. The ``payload`` field carries the
    type-specific data (one of the payload models above, serialized
    as a dict).
    """
    type: EventType
    trace_id: str
    exception_id: str
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
        node: str,
        status: NodeStatus,
        duration_ms: Optional[int] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> "WSEvent":
        payload = PipelineProgressPayload(
            node=node, status=status, duration_ms=duration_ms, data=data,
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
