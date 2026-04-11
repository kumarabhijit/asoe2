"""Exception store for the ASOE API.

Provides two backends:
  - ``ExceptionStore`` — in-memory (default when DATABASE_URL is unset)
  - ``DatabaseBackedStore`` — SQLite or PostgreSQL via ``db/repository.py``

The module-level ``exception_store`` singleton is created at import time
based on the ``DATABASE_URL`` environment variable. API routes import and
use this singleton without knowing which backend is active.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from api.schemas import ExceptionDetailResponse, ExceptionSummary
from contracts.models import STATUS_TO_LIFECYCLE


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
        lifecycle = STATUS_TO_LIFECYCLE.get(final_status or "", "INGESTED")
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


class DatabaseBackedStore:
    """Exception store backed by SQLite or PostgreSQL via db/repository.py.

    Same public interface as ExceptionStore so API routes work unchanged.
    """

    def __init__(self, database_url: str) -> None:
        from db.connection import create_adapter
        from db.repository import ExceptionRepository, TraceRepository

        self._adapter = create_adapter(database_url)
        self._adapter.apply_schema()
        self._exceptions = ExceptionRepository(self._adapter)
        self._traces = TraceRepository(self._adapter)

    def clear(self) -> None:
        # For testing: delete all records
        with self._adapter.cursor() as cur:
            cur.execute("DELETE FROM traces")
            cur.execute("DELETE FROM exceptions")

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
        row = self._exceptions.create(
            tenant_id=tenant_id,
            order_id=order_id,
            event_type=event_type,
            trace_id=trace_id,
            intent=intent,
            shadow_verdict=shadow_verdict,
            selected_recipe=selected_recipe,
            final_status=final_status,
            resolution_data=resolution_data,
        )
        return self._dict_to_record(row)

    def get(self, exception_id: str, tenant_id: str) -> Optional[ExceptionRecord]:
        row = self._exceptions.get(exception_id, tenant_id)
        if not row:
            return None
        return self._dict_to_record(row)

    def list(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        intent: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> tuple[List[ExceptionRecord], Optional[str], bool]:
        rows, next_cursor, has_more = self._exceptions.list(
            tenant_id=tenant_id, status=status, intent=intent,
            limit=limit, cursor=cursor,
        )
        records = [self._dict_to_record(r) for r in rows]
        return records, next_cursor, has_more

    def update(self, exception_id: str, tenant_id: str, **fields) -> Optional[ExceptionRecord]:
        row = self._exceptions.update(exception_id, tenant_id, **fields)
        if not row:
            return None
        return self._dict_to_record(row)

    def store_trace(self, exception_id: str, trace_data: Dict[str, Any]) -> None:
        # Retrieve the exception to get tenant_id and trace_id
        # Search across all stored records (we don't have tenant_id here)
        with self._adapter.cursor() as cur:
            cur.execute(
                "SELECT tenant_id, trace_id FROM exceptions WHERE id = ?",
                (exception_id,),
            )
            row = cur.fetchone()
        if row:
            tenant_id = row[0] if not hasattr(row, "keys") else row["tenant_id"]
            trace_id = row[1] if not hasattr(row, "keys") else row["trace_id"]
            self._traces.create(
                exception_id=exception_id,
                trace_id=trace_id,
                tenant_id=tenant_id,
                trace_record=trace_data,
            )

    def get_trace(self, exception_id: str) -> Optional[Dict[str, Any]]:
        # Look up tenant_id from exception
        with self._adapter.cursor() as cur:
            cur.execute(
                "SELECT tenant_id FROM exceptions WHERE id = ?",
                (exception_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        tenant_id = row[0] if not hasattr(row, "keys") else row["tenant_id"]
        trace_row = self._traces.get_by_exception(exception_id, tenant_id)
        if not trace_row:
            return None
        return trace_row["trace_record"]

    def stats(self, tenant_id: str) -> Dict[str, int]:
        return self._exceptions.stats(tenant_id)

    def _dict_to_record(self, d: Dict[str, Any]) -> ExceptionRecord:
        record = ExceptionRecord.__new__(ExceptionRecord)
        record.id = d["id"]
        record.tenant_id = d["tenant_id"]
        record.order_id = d["order_id"]
        record.event_type = d["event_type"]
        record.trace_id = d["trace_id"]
        record.intent = d.get("intent")
        record.lifecycle_state = d.get("lifecycle_state", "INGESTED")
        record.shadow_verdict = d.get("shadow_verdict")
        record.selected_recipe = d.get("selected_recipe")
        record.final_status = d.get("final_status")
        record.resolution_data = d.get("resolution_data") or {}
        record.resolved_by = d.get("resolved_by")
        record.resolved_action = d.get("resolved_action")
        record.resolution_notes = d.get("resolution_notes")
        record.created_at = d.get("created_at", "")
        record.updated_at = d.get("updated_at", "")
        return record


def _create_store():
    """Create the appropriate store based on DATABASE_URL."""
    import os
    database_url = os.getenv("DATABASE_URL", "")
    if database_url:
        return DatabaseBackedStore(database_url)
    return ExceptionStore()


# Module-level singleton — uses DATABASE_URL when set, else in-memory.
exception_store = _create_store()
