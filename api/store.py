"""In-memory exception store for V1 API.

Provides persistence for exception records created during graph execution.
This will be replaced by PostgreSQL (architecture_v3.md Section 9.2) when
the database layer is built. The interface is intentionally simple so the
migration is straightforward.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from api.schemas import ExceptionDetailResponse, ExceptionSummary


class ExceptionRecord:
    """In-memory representation of a persisted exception."""

    def __init__(
        self,
        tenant_id: str,
        order_id: str,
        event_type: str,
        trace_id: str,
        intent: Optional[str] = None,
        lifecycle_state: str = "INGESTED",
        shadow_verdict: Optional[str] = None,
        selected_recipe: Optional[str] = None,
        final_status: Optional[str] = None,
        resolution_data: Optional[Dict[str, Any]] = None,
        resolved_by: Optional[str] = None,
        resolved_action: Optional[str] = None,
        resolution_notes: Optional[str] = None,
    ):
        self.id = str(uuid4())
        self.tenant_id = tenant_id
        self.order_id = order_id
        self.event_type = event_type
        self.trace_id = trace_id
        self.intent = intent
        self.lifecycle_state = lifecycle_state
        self.shadow_verdict = shadow_verdict
        self.selected_recipe = selected_recipe
        self.final_status = final_status
        self.resolution_data = resolution_data or {}
        self.resolved_by = resolved_by
        self.resolved_action = resolved_action
        self.resolution_notes = resolution_notes
        now = datetime.now(timezone.utc).isoformat()
        self.created_at = now
        self.updated_at = now

    def to_summary(self) -> ExceptionSummary:
        return ExceptionSummary(
            id=self.id,
            tenant_id=self.tenant_id,
            order_id=self.order_id,
            event_type=self.event_type,
            intent=self.intent,
            lifecycle_state=self.lifecycle_state,
            shadow_verdict=self.shadow_verdict,
            selected_recipe=self.selected_recipe,
            final_status=self.final_status,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    def to_detail(self) -> ExceptionDetailResponse:
        return ExceptionDetailResponse(
            id=self.id,
            tenant_id=self.tenant_id,
            order_id=self.order_id,
            event_type=self.event_type,
            intent=self.intent,
            lifecycle_state=self.lifecycle_state,
            shadow_verdict=self.shadow_verdict,
            selected_recipe=self.selected_recipe,
            final_status=self.final_status,
            trace_id=self.trace_id,
            resolution_data=self.resolution_data,
            resolved_by=self.resolved_by,
            resolved_action=self.resolved_action,
            resolution_notes=self.resolution_notes,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


# Maps final_status to lifecycle_state
_STATUS_TO_LIFECYCLE = {
    "COMPLETE": "RESOLVED",
    "FAIL_TO_HUMAN": "FAILED",
    "MANUAL_REVIEW_REQUIRED": "PENDING_REVIEW",
    "BLOCKED": "BLOCKED",
    "REJECTED": "REJECTED",
}


class ExceptionStore:
    """Thread-safe in-memory exception store."""

    def __init__(self) -> None:
        self._records: Dict[str, ExceptionRecord] = {}
        self._traces: Dict[str, Dict[str, Any]] = {}  # exception_id → trace data
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._traces.clear()

    def create(
        self,
        tenant_id: str,
        order_id: str,
        event_type: str,
        trace_id: str,
        intent: Optional[str] = None,
        shadow_verdict: Optional[str] = None,
        selected_recipe: Optional[str] = None,
        final_status: Optional[str] = None,
        resolution_data: Optional[Dict[str, Any]] = None,
    ) -> ExceptionRecord:
        lifecycle = _STATUS_TO_LIFECYCLE.get(final_status or "", "INGESTED")
        record = ExceptionRecord(
            tenant_id=tenant_id,
            order_id=order_id,
            event_type=event_type,
            trace_id=trace_id,
            intent=intent,
            lifecycle_state=lifecycle,
            shadow_verdict=shadow_verdict,
            selected_recipe=selected_recipe,
            final_status=final_status,
            resolution_data=resolution_data,
        )
        with self._lock:
            self._records[record.id] = record
        return record

    def get(self, exception_id: str, tenant_id: str) -> Optional[ExceptionRecord]:
        with self._lock:
            record = self._records.get(exception_id)
        if record and record.tenant_id == tenant_id:
            return record
        return None

    def list(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        intent: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> tuple[List[ExceptionRecord], Optional[str], bool]:
        with self._lock:
            records = [
                r for r in self._records.values()
                if r.tenant_id == tenant_id
            ]

        if status:
            records = [r for r in records if r.lifecycle_state == status]
        if intent:
            records = [r for r in records if r.intent == intent]

        records.sort(key=lambda r: r.created_at, reverse=True)

        # Simple cursor-based pagination using record ID
        if cursor:
            idx = next(
                (i for i, r in enumerate(records) if r.id == cursor),
                None,
            )
            if idx is not None:
                records = records[idx + 1 :]

        has_more = len(records) > limit
        page = records[:limit]
        next_cursor = page[-1].id if has_more and page else None
        return page, next_cursor, has_more

    def update(self, exception_id: str, tenant_id: str, **fields) -> Optional[ExceptionRecord]:
        with self._lock:
            record = self._records.get(exception_id)
            if not record or record.tenant_id != tenant_id:
                return None
            for key, value in fields.items():
                if hasattr(record, key):
                    setattr(record, key, value)
            record.updated_at = datetime.now(timezone.utc).isoformat()
            return record

    def store_trace(self, exception_id: str, trace_data: Dict[str, Any]) -> None:
        with self._lock:
            self._traces[exception_id] = trace_data

    def get_trace(self, exception_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._traces.get(exception_id)

    def stats(self, tenant_id: str) -> Dict[str, int]:
        with self._lock:
            records = [
                r for r in self._records.values()
                if r.tenant_id == tenant_id
            ]
        return {
            "total": len(records),
            "open": sum(1 for r in records if r.lifecycle_state in ("INGESTED", "CLASSIFYING", "AUDITING", "EXECUTING")),
            "auto_resolved": sum(1 for r in records if r.lifecycle_state == "RESOLVED"),
            "manual_review": sum(1 for r in records if r.lifecycle_state == "PENDING_REVIEW"),
            "blocked": sum(1 for r in records if r.lifecycle_state == "BLOCKED"),
            "failed": sum(1 for r in records if r.lifecycle_state == "FAILED"),
        }


# Module-level singleton — replaced by DI when PostgreSQL is available.
exception_store = ExceptionStore()
