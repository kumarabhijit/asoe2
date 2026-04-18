"""Exception store for the ASOE API.

Provides two backends:
  - ``ExceptionStore`` — in-memory (default when DATABASE_URL is unset)
  - ``DatabaseBackedStore`` — SQLite or PostgreSQL via ``db/repository.py``

The module-level ``exception_store`` singleton is created at import time
based on the ``DATABASE_URL`` environment variable. API routes import and
use this singleton without knowing which backend is active.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger("asoe.api.store")

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
        account_id: Optional[str] = None,
        account_name: Optional[str] = None,
        original_event: Optional[Dict[str, Any]] = None,
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
        self.account_id = account_id
        self.account_name = account_name
        # Original OrderEvent payload, captured at create time so a later
        # re-analysis can faithfully replay through run_graph(). None for
        # records created before the feature shipped.
        self.original_event: Optional[Dict[str, Any]] = original_event
        # Append-only audit trail of human-triggered re-analyses. Each entry:
        # {attempt, triggered_at, triggered_by, reason, prior_trace_id,
        #  prior_shadow_verdict, prior_final_status, new_trace_id,
        #  new_shadow_verdict, new_final_status}
        self.reanalysis_history: List[Dict[str, Any]] = []
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
            account_id=self.account_id,
            account_name=self.account_name,
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
            account_id=self.account_id,
            account_name=self.account_name,
            trace_id=self.trace_id,
            resolution_data=self.resolution_data,
            resolved_by=self.resolved_by,
            resolved_action=self.resolved_action,
            resolution_notes=self.resolution_notes,
            reanalysis_history=list(self.reanalysis_history),
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
            self._audit_log: List[Dict[str, Any]] = []

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
        original_event: Optional[Dict[str, Any]] = None,
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
            original_event=original_event,
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

    def append_reanalysis(
        self,
        exception_id: str,
        tenant_id: str,
        entry: Dict[str, Any],
    ) -> Optional[ExceptionRecord]:
        """Append an immutable entry to the reanalysis_history list.

        Entries are append-only — callers must not mutate existing entries.
        """
        with self._lock:
            record = self._records.get(exception_id)
            if not record or record.tenant_id != tenant_id:
                return None
            record.reanalysis_history.append(entry)
            record.updated_at = datetime.now(timezone.utc).isoformat()
            return record

    def log_audit_event(
        self,
        tenant_id: str,
        policy_key: str,
        previous_value: Any,
        new_value: Any,
        changed_by: str,
        change_reason: Optional[str] = None,
    ) -> None:
        """Record an immutable audit event (SOX compliance).

        Phase 3 #3: hash-chained. Each entry carries
          event_hash = sha256(prev_hash || canonical_event_json)
        so a later reader can verify that nothing was deleted or mutated
        between events. `prev_hash` for the first event in a tenant's log
        is the literal string "GENESIS".

        In-memory store: appends to _audit_log list.
        Database store: inserts into policy_audit_log table (hash columns
        to be added in a follow-up migration).
        """
        import hashlib
        import json

        with self._lock:
            if not hasattr(self, "_audit_log"):
                self._audit_log: List[Dict[str, Any]] = []
            # Find the last event for THIS tenant; chains are per-tenant so
            # a bad actor can't cross-contaminate another tenant's log.
            prev_hash = "GENESIS"
            for e in reversed(self._audit_log):
                if e["tenant_id"] == tenant_id:
                    prev_hash = e["event_hash"]
                    break
            event = {
                "id": str(uuid4()),
                "tenant_id": tenant_id,
                "policy_key": policy_key,
                "previous_value": previous_value,
                "new_value": new_value,
                "changed_by": changed_by,
                "change_reason": change_reason,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "prev_hash": prev_hash,
            }
            # Canonical JSON so reordering keys doesn't change the hash.
            payload = json.dumps(
                {k: event[k] for k in sorted(event) if k != "event_hash"},
                sort_keys=True,
                default=str,
            )
            event["event_hash"] = hashlib.sha256(
                (prev_hash + "|" + payload).encode("utf-8")
            ).hexdigest()
            self._audit_log.append(event)
        logger.info(
            "Audit event: %s by %s — %s",
            policy_key, changed_by, change_reason,
        )

    def get_audit_log(self, tenant_id: str) -> List[Dict[str, Any]]:
        """Return audit events for a tenant (for testing)."""
        with self._lock:
            log = getattr(self, "_audit_log", [])
            return [e for e in log if e["tenant_id"] == tenant_id]

    def verify_audit_chain(self, tenant_id: str) -> tuple[bool, Optional[int]]:
        """Phase 3 #3 — walk the tenant's audit chain and verify hashes.

        Returns (is_valid, first_break_index). A valid chain returns
        (True, None). A tamper-evident break (edit/delete) returns
        (False, i) where i is the zero-based index of the first event
        whose recomputed hash does not match its stored event_hash or
        whose prev_hash does not match the prior event's event_hash.
        """
        import hashlib
        import json

        events = self.get_audit_log(tenant_id)
        expected_prev = "GENESIS"
        for i, e in enumerate(events):
            if e.get("prev_hash") != expected_prev:
                return False, i
            payload = json.dumps(
                {k: e[k] for k in sorted(e) if k != "event_hash"},
                sort_keys=True,
                default=str,
            )
            expected = hashlib.sha256(
                (e["prev_hash"] + "|" + payload).encode("utf-8")
            ).hexdigest()
            if e.get("event_hash") != expected:
                return False, i
            expected_prev = e["event_hash"]
        return True, None

    def stats(self, tenant_id: str) -> Dict[str, Any]:
        with self._lock:
            records = [
                r for r in self._records.values()
                if r.tenant_id == tenant_id
            ]
        _OPEN_STATES = {"INGESTED", "CLASSIFYING", "AUDITING"}
        open_count = auto_resolved = manual_review = blocked = failed = 0
        by_intent: Dict[str, int] = {}
        by_lifecycle_state: Dict[str, int] = {}
        by_shadow_verdict: Dict[str, int] = {}
        resolved_times: list[float] = []
        for r in records:
            s = r.lifecycle_state
            if s in _OPEN_STATES:
                open_count += 1
            elif s == "RESOLVED":
                auto_resolved += 1
                # Compute resolution time for resolved exceptions
                try:
                    created = datetime.fromisoformat(r.created_at)
                    updated = datetime.fromisoformat(r.updated_at)
                    resolved_times.append((updated - created).total_seconds())
                except (ValueError, TypeError):
                    pass
            elif s == "PENDING_REVIEW":
                manual_review += 1
            elif s == "BLOCKED":
                blocked += 1
            elif s == "FAILED":
                failed += 1

            # Aggregate by intent
            intent_key = r.intent or "UNKNOWN"
            by_intent[intent_key] = by_intent.get(intent_key, 0) + 1

            # Aggregate by lifecycle state
            by_lifecycle_state[s] = by_lifecycle_state.get(s, 0) + 1

            # Aggregate by shadow verdict
            if r.shadow_verdict:
                by_shadow_verdict[r.shadow_verdict] = by_shadow_verdict.get(r.shadow_verdict, 0) + 1

        avg_resolution: Optional[float] = None
        if resolved_times:
            avg_resolution = sum(resolved_times) / len(resolved_times)

        return {
            "total_exceptions": len(records),
            "open_exceptions": open_count,
            "auto_resolved": auto_resolved,
            "manual_review": manual_review,
            "blocked": blocked,
            "failed": failed,
            "avg_resolution_time_seconds": avg_resolution,
            "by_intent": by_intent,
            "by_lifecycle_state": by_lifecycle_state,
            "by_shadow_verdict": by_shadow_verdict,
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
        original_event: Optional[Dict[str, Any]] = None,
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
            # V002 promoted these to dedicated columns. The repository
            # serialises to JSONB (Postgres) / JSON TEXT (SQLite).
            original_event=original_event,
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

    def append_reanalysis(
        self,
        exception_id: str,
        tenant_id: str,
        entry: Dict[str, Any],
    ) -> Optional[ExceptionRecord]:
        """Append an entry to the reanalysis_history JSON column."""
        current = self._exceptions.get(exception_id, tenant_id)
        if not current:
            return None
        history = list(current.get("reanalysis_history") or [])
        history.append(entry)
        row = self._exceptions.update(
            exception_id, tenant_id, reanalysis_history=history,
        )
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

    def log_audit_event(
        self,
        tenant_id: str,
        policy_key: str,
        previous_value: Any,
        new_value: Any,
        changed_by: str,
        change_reason: Optional[str] = None,
    ) -> None:
        """Record an immutable audit event to policy_audit_log (SOX)."""
        try:
            from db.repository import PolicyRepository
            repo = PolicyRepository(self._adapter)
            repo.create_override(
                tenant_id=tenant_id,
                policy_key=policy_key,
                value=new_value,
                created_by=changed_by,
                change_reason=change_reason,
            )
        except Exception:
            # Fallback: log to stdlib if DB write fails
            logger.warning(
                "Failed to write audit event to DB: %s by %s",
                policy_key, changed_by,
            )

    def get_audit_log(self, tenant_id: str) -> List[Dict[str, Any]]:
        """Return audit events for a tenant."""
        try:
            from db.repository import PolicyRepository
            repo = PolicyRepository(self._adapter)
            return repo.list_audit_log(tenant_id)
        except Exception:
            return []

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
        record.account_id = d.get("account_id")
        record.account_name = d.get("account_name")
        record.original_event = d.get("original_event")
        record.reanalysis_history = d.get("reanalysis_history") or []
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
