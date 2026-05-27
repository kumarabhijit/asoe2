"""Child-case store for the ASOE API.

Provides two backends over the ``exceptions`` table (V001):
  - ``ExceptionStore`` — in-memory (default when DATABASE_URL is unset)
  - ``DatabaseBackedStore`` — SQLite or PostgreSQL via ``db/repository.py``

The store classes and the module-level ``exception_store`` singleton are
named after the underlying DB table (``exceptions``, V001). The row
class is ``ChildCase`` per the Case & Intent Super-Group requirements
§3 glossary — the data is a child-of-OrderCase, regardless of where it
lives on disk. The table name is retained for migration safety.

API routes import the singleton and don't know which backend is active.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger("asoe.api.store")

from api.schemas import ExceptionDetailResponse, ExceptionSummary
from contracts._generated.taxonomy_constants import (
    SG_NEEDS_TRIAGE,
    TAXONOMY_VERSION,
)
from contracts.models import STATUS_TO_LIFECYCLE, ClassificationEvent


# Statuses that count as "case closed" for the NEEDS_TRIAGE forcing
# function (requirements §8.2). Today the spec names only "RESOLVED"
# — declared as a set so a future closed-like status (e.g. RESOLVED_
# WITH_OVERRIDE) is one place to add it.
_RESOLVED_LIKE: frozenset[str] = frozenset({"RESOLVED"})


class NeedsTriageCloseBlocked(Exception):
    """Raised when a case in ``SG_NEEDS_TRIAGE`` is asked to RESOLVE.

    Requirements §8.2 forcing function #1 — hard-block at close.
    Inherits from ``Exception`` directly (not ``ValueError``): the
    failure is structural, not a value-validation error, and callers
    must handle it explicitly rather than catching a broader
    ValueError net (CLAUDE.md §5 — failures are explicit).
    """


class ChildCase:
    """In-memory representation of a child case (DB table: ``exceptions``)."""

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
        enrichment_context: Optional[Dict[str, Any]] = None,
        parent_case_id: Optional[str] = None,
        sap_block_code: Optional[str] = None,
        # Case & Intent Super-Group fields (requirements §5).
        # Optional carriers in Phase 2; populated by Phase 3 classifier wiring.
        supergroup_code: Optional[str] = None,
        intent_code: Optional[str] = None,
        divergence_reason: Optional[str] = None,
        sap_block_field: Optional[str] = None,
        scope: Optional[str] = None,
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
        # ADR-038 Phase H.2 — parent OrderCase. None for Tier-1 stateless
        # records (clean Automated events that auto-resolve to COMPLETE
        # without ever materialising a case). Populated for every T2/T3
        # record once Phase H.3 wires lazy materialisation in
        # orchestration/nodes.py::build_analysis.
        self.parent_case_id: Optional[str] = parent_case_id
        # Requirements §5 — raw SAP block reason code on records whose
        # parent case is ``origin='API'``. Distinct from ``intent_code``
        # (the classified leaf intent): ``intent_code`` is the routing
        # key recipes dispatch on; ``sap_block_code`` is the source
        # signal for audit ("SAP reported this exact code"). None on
        # CUSTOMER-origin records.
        self.sap_block_code: Optional[str] = sap_block_code
        # Case & Intent Super-Group fields (requirements §5).
        # `supergroup_code` mirrors the parent OrderCase classification
        # (strict inheritance per §8.1). `intent_code` is the leaf
        # routing key from the case_intent lookup. Both nullable in
        # Phase 2 until the classifier wiring lands in Phase 3.
        self.supergroup_code: Optional[str] = supergroup_code
        self.intent_code: Optional[str] = intent_code
        self.divergence_reason: Optional[str] = divergence_reason
        self.sap_block_field: Optional[str] = sap_block_field
        self.scope: Optional[str] = scope
        # Original OrderEvent payload, captured at create time so a later
        # re-analysis can faithfully replay through run_graph(). None for
        # records created before the feature shipped.
        self.original_event: Optional[Dict[str, Any]] = original_event
        # Verdict Pillar 1 (2026-04-22 compliance workshop): audit-bearing
        # upstream context carried from GraphState.enrichment_context.
        # Gateway-fetched evidence (matched POs, warehouse snapshots,
        # contract refs, SAP doc numbers) that the operator reviewed to
        # authorise an action. The build_analysis composition node reads
        # this to populate Layer-2 evidence on the UI. Distinct from
        # resolution_data (recipe output). Empty = "not fetched for this
        # resolution path" — triggers "Context Not Required for
        # Resolution" on the UI, never a dash.
        self.enrichment_context: Dict[str, Any] = enrichment_context or {}
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
            parent_case_id=self.parent_case_id,
            supergroup_code=self.supergroup_code,
            intent_code=self.intent_code,
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
            parent_case_id=self.parent_case_id,
            supergroup_code=self.supergroup_code,
            intent_code=self.intent_code,
            divergence_reason=self.divergence_reason,
            sap_block_field=self.sap_block_field,
            scope=self.scope,
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
        self._records: Dict[str, ChildCase] = {}
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
        enrichment_context: Optional[Dict[str, Any]] = None,
        parent_case_id: Optional[str] = None,
        # V018 — symmetric with DatabaseBackedStore.create (finding #4).
        supergroup_code: Optional[str] = None,
        intent_code: Optional[str] = None,
        divergence_reason: Optional[str] = None,
        sap_block_field: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> ChildCase:
        lifecycle = STATUS_TO_LIFECYCLE.get(final_status or "", "INGESTED")
        record = ChildCase(
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
            enrichment_context=enrichment_context,
            parent_case_id=parent_case_id,
            supergroup_code=supergroup_code,
            intent_code=intent_code,
            divergence_reason=divergence_reason,
            sap_block_field=sap_block_field,
            scope=scope,
        )
        with self._lock:
            self._records[record.id] = record
        return record

    def get(self, exception_id: str, tenant_id: str) -> Optional[ChildCase]:
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
    ) -> tuple[List[ChildCase], Optional[str], bool]:
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

    def list_by_case(
        self, tenant_id: str, case_id: str,
    ) -> List[ChildCase]:
        """ADR-038 Phase H.6 — children-of-case lookup for the
        ``/api/v1/cases/*`` partner-scope helper. The case itself
        carries no ``account_id``; scope is derived from its
        children."""
        with self._lock:
            return [
                r for r in self._records.values()
                if r.tenant_id == tenant_id and r.parent_case_id == case_id
            ]

    def update(self, exception_id: str, tenant_id: str, **fields) -> Optional[ChildCase]:
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
    ) -> Optional[ChildCase]:
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
        *,
        strict: bool = False,  # noqa: ARG002 - signature parity with DB store
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

        ``strict`` is accepted for signature parity with the DB-backed
        variant (PARITY-0.5 / Review 3). The in-memory store can't fail
        the append silently — a programming error raises naturally — so
        the flag is a no-op here.
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
        enrichment_context: Optional[Dict[str, Any]] = None,
        parent_case_id: Optional[str] = None,
        # V018 — Case & Intent Super-Group fields (requirements §5).
        # Forwarded into the repository so DB-backed writes persist them
        # from day one (code-review finding #4). Phase 3 classifier
        # wiring starts setting them; pre-Phase-3 callers leave them
        # None and the columns stay NULL on the row.
        supergroup_code: Optional[str] = None,
        intent_code: Optional[str] = None,
        divergence_reason: Optional[str] = None,
        sap_block_field: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> ChildCase:
        # Verdict Pillar 1: `enrichment_context` is persisted to a
        # dedicated JSONB column (V004). The repository serialises
        # to JSONB (Postgres) / JSON TEXT (SQLite) alongside
        # `original_event` (V002).
        #
        # ADR-038 Phase H.3: parent_case_id is accepted on this
        # signature for API compat with ExceptionStore. The
        # DB-backed repository persistence of this column lands in
        # Phase H.7 alongside the orphan-case backfill; until then
        # the value is captured on the in-memory record only.
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
            original_event=original_event,
            enrichment_context=enrichment_context,
            supergroup_code=supergroup_code,
            intent_code=intent_code,
            divergence_reason=divergence_reason,
            sap_block_field=sap_block_field,
            scope=scope,
        )
        record = self._dict_to_record(row)
        record.parent_case_id = parent_case_id
        return record

    def get(self, exception_id: str, tenant_id: str) -> Optional[ChildCase]:
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
    ) -> tuple[List[ChildCase], Optional[str], bool]:
        rows, next_cursor, has_more = self._exceptions.list(
            tenant_id=tenant_id, status=status, intent=intent,
            limit=limit, cursor=cursor,
        )
        records = [self._dict_to_record(r) for r in rows]
        return records, next_cursor, has_more

    def list_by_case(
        self, tenant_id: str, case_id: str,
    ) -> List[ChildCase]:
        """Return all child exception records linked to a case.

        ADR-038 Phase H.6 — used by `/api/v1/cases/*` to derive
        partner-role / assigned-account scope from the case's
        children, since the case itself doesn't carry account_id.
        """
        rows, _, _ = self._exceptions.list(
            tenant_id=tenant_id, limit=500,
        )
        records = [self._dict_to_record(r) for r in rows]
        return [r for r in records if r.parent_case_id == case_id]

    def update(self, exception_id: str, tenant_id: str, **fields) -> Optional[ChildCase]:
        row = self._exceptions.update(exception_id, tenant_id, **fields)
        if not row:
            return None
        return self._dict_to_record(row)

    def append_reanalysis(
        self,
        exception_id: str,
        tenant_id: str,
        entry: Dict[str, Any],
    ) -> Optional[ChildCase]:
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
        *,
        strict: bool = False,
    ) -> None:
        """Record an immutable audit event to policy_audit_log (SOX).

        Phase 4: routes through PolicyRepository.create_audit_event so the
        DB-side hash chain is updated on every write. Previously this
        called create_override, which incorrectly inserted a stray row
        into policy_overrides for application-level events like
        EXCEPTION_RESOLVED.

        PARITY-0.5 (Review 3): when ``strict=True`` the DB-write failure
        re-raises. Callers that depend on the audit row being present
        before another irreversible action proceeds (e.g.,
        ``erase_attachment`` deleting the bytes) MUST pass
        ``strict=True`` to preserve the proof-of-erasure invariant.
        Default ``strict=False`` keeps the existing swallow-and-warn
        behaviour for non-critical audit events.
        """
        try:
            from db.repository import PolicyRepository
            repo = PolicyRepository(self._adapter)
            repo.create_audit_event(
                tenant_id=tenant_id,
                policy_key=policy_key,
                previous_value=previous_value,
                new_value=new_value,
                changed_by=changed_by,
                change_reason=change_reason,
            )
        except Exception:
            # Fallback: log to stdlib if DB write fails
            logger.warning(
                "Failed to write audit event to DB: %s by %s",
                policy_key, changed_by,
            )
            if strict:
                # PARITY-0.5: a strict caller needs the chain row to be
                # provable; re-raise so the caller can roll back the
                # paired action (e.g., abort the byte-delete).
                raise

    def get_audit_log(self, tenant_id: str) -> List[Dict[str, Any]]:
        """Return audit events for a tenant."""
        try:
            from db.repository import PolicyRepository
            repo = PolicyRepository(self._adapter)
            return repo.list_audit_log(tenant_id)
        except Exception:
            return []

    def _dict_to_record(self, d: Dict[str, Any]) -> ChildCase:
        record = ChildCase.__new__(ChildCase)
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
        # Verdict Pillar 1: enrichment_context is durable (V004 column).
        record.enrichment_context = d.get("enrichment_context") or {}
        record.reanalysis_history = d.get("reanalysis_history") or []
        record.created_at = d.get("created_at", "")
        record.updated_at = d.get("updated_at", "")
        # V009 + V018 columns. Optional in Phase 2 (legacy rows are NULL).
        record.parent_case_id = d.get("parent_case_id")
        record.sap_block_code = d.get("sap_block_code")
        record.supergroup_code = d.get("supergroup_code")
        record.intent_code = d.get("intent_code")
        record.divergence_reason = d.get("divergence_reason")
        record.sap_block_field = d.get("sap_block_field")
        record.scope = d.get("scope")
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


# =====================================================================
# ADR-038 Phase H.2 — OrderCase store + correlation table (in-memory)
# =====================================================================
#
# Parallel to ExceptionStore. Lookup-or-create on inbound events keys
# off the four correlation identifiers (sales_order_id → customer_po
# → edi_transaction_id → source_email_id; first match wins). The
# in-memory implementation here exercises the API surface; the SQL-
# backed equivalent (V009 + V010 migrations) is wired in Phase H.7
# alongside the orphan-case backfill.


class CaseStore:
    """Thread-safe in-memory `OrderCase` store.

    Key responsibilities (ADR-038 §6.2):
      * `lookup_or_create(...)` resolves an inbound event to an existing
        case via the four correlation key types or opens a new case
        registering whatever keys the event carried.
      * `register_correlation(...)` adds a new key to an existing case
        (e.g., the ERP creates the SO and the new key joins the case).
      * `get(case_id)` / `update(case_id, ...)` / `list_by_tenant(...)`
        provide CRUD for the harness and read APIs.
    """

    def __init__(self) -> None:
        self._cases: Dict[str, "OrderCase"] = {}
        # Correlation index: {(tenant_id, key_type, key_value): case_id}
        self._correlation: Dict[tuple, str] = {}
        # Append-only classification audit per case_id. Mirrors the
        # ``case_classification_history`` table (V020). Requirements §8.6
        # — every classification event lands here as a new row; we never
        # mutate or remove. The DB-backed store will read from the V020
        # table; the in-memory store keeps this list authoritative.
        self._history: Dict[str, List["ClassificationEvent"]] = {}
        self._lock = threading.RLock()

    # ----- correlation lookup --------------------------------------

    def find_by_correlation(
        self,
        tenant_id: str,
        *,
        sales_order_id: Optional[str] = None,
        customer_po_number: Optional[str] = None,
        edi_transaction_id: Optional[str] = None,
        source_email_id: Optional[str] = None,
    ) -> Optional["OrderCase"]:
        """Return the case matching any of the supplied correlation
        keys. Resolution priority follows ADR-038 §6.2: SO → PO →
        EDI txn → email. First match wins. ``None`` if no match.
        """
        priority = (
            ("sales_order_id", sales_order_id),
            ("customer_po_number", customer_po_number),
            ("edi_transaction_id", edi_transaction_id),
            ("source_email_id", source_email_id),
        )
        with self._lock:
            for key_type, key_value in priority:
                if not key_value:
                    continue
                case_id = self._correlation.get((tenant_id, key_type, key_value))
                if case_id is not None:
                    return self._cases.get(case_id)
        return None

    # ----- lookup-or-create ----------------------------------------

    def lookup_or_create(
        self,
        tenant_id: str,
        *,
        origin: str,
        source_channel: str,
        customer_id: Optional[str] = None,
        customer_po_number: Optional[str] = None,
        sales_order_id: Optional[str] = None,
        edi_transaction_id: Optional[str] = None,
        source_email_id: Optional[str] = None,
        sla_deadline: Optional[str] = None,
        bundle_version_at_open: Optional[str] = None,
        supergroup_code: Optional[str] = None,
        intent_code: Optional[str] = None,
        classified_by: str = "system:case_intake",
        classifier_type: str = "RULE",
    ) -> tuple["OrderCase", bool]:
        """Resolve an inbound event to a case (existing or new).

        Returns ``(case, opened_now)`` where ``opened_now=True`` when
        a new case was opened on this call. ADR-038 §6.2 multi-PO note:
        callers split a multi-PO email into N invocations of this
        method (one per PO); each opens or attaches to its own case.

        Audit trail (requirements §8.6, criterion #9): when a new case
        is opened with ``supergroup_code`` set, exactly one
        ``ClassificationEvent`` row is appended to ``_history``. The
        intake path defaults are ``classifier_type='RULE'``,
        ``classified_by='system:case_intake'`` — overridable by the
        caller when the intake path itself ran a classifier (MODEL) or
        a human (HUMAN) chose the value.
        """
        existing = self.find_by_correlation(
            tenant_id,
            sales_order_id=sales_order_id,
            customer_po_number=customer_po_number,
            edi_transaction_id=edi_transaction_id,
            source_email_id=source_email_id,
        )
        with self._lock:
            if existing is not None:
                # Enrich correlation keys with any new identifiers this
                # event carried. The case ``origin`` is immutable;
                # ``source_channel`` is the existing one (don't overwrite
                # — first event wins for the origin/channel pair). New
                # correlation keys join the case.
                self._register_correlations_locked(
                    tenant_id, existing.case_id,
                    sales_order_id=sales_order_id,
                    customer_po_number=customer_po_number,
                    edi_transaction_id=edi_transaction_id,
                    source_email_id=source_email_id,
                )
                return existing, False

            from contracts.models import OrderCase  # local to avoid cycles
            case = OrderCase(
                tenant_id=tenant_id,
                customer_id=customer_id,
                origin=origin,  # type: ignore[arg-type]
                source_channel=source_channel,
                supergroup_code=supergroup_code,
                customer_po_number=customer_po_number,
                sales_order_id=sales_order_id,
                edi_transaction_id=edi_transaction_id,
                source_email_id=source_email_id,
                sla_deadline=sla_deadline,
                bundle_version_at_open=bundle_version_at_open,
            )
            self._cases[case.case_id] = case
            self._register_correlations_locked(
                tenant_id, case.case_id,
                sales_order_id=sales_order_id,
                customer_po_number=customer_po_number,
                edi_transaction_id=edi_transaction_id,
                source_email_id=source_email_id,
            )
            # Audit trail: record the intake classification so criterion
            # #9 is satisfied at the data-store boundary. No-op when the
            # case opened without a supergroup_code (Phase 5 will fill
            # those in via a later record_classification call once the
            # classifier wires through).
            if supergroup_code is not None:
                self._record_classification_locked(
                    case_id=case.case_id,
                    supergroup_code=supergroup_code,
                    intent_code=intent_code,
                    classified_by=classified_by,
                    classifier_type=classifier_type,
                )
            return case, True

    def _register_correlations_locked(
        self,
        tenant_id: str,
        case_id: str,
        *,
        sales_order_id: Optional[str] = None,
        customer_po_number: Optional[str] = None,
        edi_transaction_id: Optional[str] = None,
        source_email_id: Optional[str] = None,
    ) -> None:
        """Append correlation rows for the case. Caller holds the lock."""
        for key_type, key_value in (
            ("sales_order_id", sales_order_id),
            ("customer_po_number", customer_po_number),
            ("edi_transaction_id", edi_transaction_id),
            ("source_email_id", source_email_id),
        ):
            if not key_value:
                continue
            self._correlation[(tenant_id, key_type, key_value)] = case_id

    def register_correlation(
        self,
        tenant_id: str,
        case_id: str,
        key_type: str,
        key_value: str,
    ) -> None:
        """Register a single new correlation key after case open
        (e.g., ERP creates the SO and the SO id joins the case)."""
        with self._lock:
            if case_id not in self._cases:
                raise KeyError(f"Unknown case_id: {case_id}")
            self._correlation[(tenant_id, key_type, key_value)] = case_id

    # ----- CRUD ----------------------------------------------------

    def get(self, case_id: str) -> Optional["OrderCase"]:
        return self._cases.get(case_id)

    def update(
        self,
        case_id: str,
        *,
        classified_by: Optional[str] = None,
        classifier_type: Optional[str] = None,
        intent_code_classification: Optional[str] = None,
        model_version: Optional[str] = None,
        reason_text: Optional[str] = None,
        source_event_id: Optional[str] = None,
        **fields: Any,
    ) -> "OrderCase":
        """Apply field updates and return the updated case.

        Pydantic model is recreated via `model_copy(update=...)` so
        validators fire. Raises `KeyError` if case_id is unknown.

        Issue #133 PO #17 — `updated_at` is bumped to "now" whenever
        a mutation lands, unless the caller passes an explicit
        ``updated_at`` (audit replay, sandbox seeding). Callers
        therefore never need to remember to set it.

        NEEDS_TRIAGE close-block (requirements §8.2): a case whose
        supergroup_code is the reserved ``SG_NEEDS_TRIAGE`` value
        cannot transition to ``RESOLVED``. Callers must reclassify
        the case first. Raises ``NeedsTriageCloseBlocked`` so the
        failure is explicit (CLAUDE.md §5: "Explicit failure is
        correct behavior").

        Audit trail (requirements §8.6, criterion #9): an update that
        touches ``supergroup_code`` is a reclassification event.
        Callers MUST pass ``classified_by`` and ``classifier_type``
        kwargs in that case; the method appends one
        ``ClassificationEvent`` row to the in-memory audit trail
        atomically with the state mutation. Updates that don't touch
        ``supergroup_code`` are pure field updates and don't require
        the audit kwargs.

        Confidence gate: when ``classifier_type='MODEL'`` is used here
        or in ``record_classification``, the caller must have already
        verified that the model's confidence is ≥ 0.85 per
        requirements §8.3. The gate is enforced at the call site
        (skills/intent_classifier.py, Phase 5) — this store does not
        re-check.
        """
        with self._lock:
            existing = self._cases.get(case_id)
            if existing is None:
                raise KeyError(f"Unknown case_id: {case_id}")
            target_status = fields.get("status", existing.status)
            target_sg = fields.get("supergroup_code", existing.supergroup_code)
            sg_is_reclassified = (
                "supergroup_code" in fields
                and fields["supergroup_code"] != existing.supergroup_code
            )
            # Audit-kwarg requirement: every change to supergroup_code
            # must be attributable.
            if sg_is_reclassified:
                if not classified_by or not classifier_type:
                    raise ValueError(
                        f"update(case_id={case_id}) changes supergroup_code "
                        "but did not supply classified_by + classifier_type; "
                        "every reclassification must be attributable "
                        "(requirements §8.6)."
                    )
            if target_status in _RESOLVED_LIKE and target_sg == SG_NEEDS_TRIAGE:
                raise NeedsTriageCloseBlocked(
                    f"Case {case_id} is in SG_NEEDS_TRIAGE and cannot be "
                    "RESOLVED without reclassification (requirements §8.2)."
                )
            if "updated_at" not in fields:
                fields = {
                    **fields,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            updated = existing.model_copy(update=fields)
            self._cases[case_id] = updated
            if sg_is_reclassified:
                # classifier_type / classified_by are guaranteed non-None
                # by the guard above; appease the type checker.
                assert classified_by is not None
                assert classifier_type is not None
                self._record_classification_locked(
                    case_id=case_id,
                    supergroup_code=updated.supergroup_code or "",
                    intent_code=intent_code_classification,
                    classified_by=classified_by,
                    classifier_type=classifier_type,
                    model_version=model_version,
                    reason_text=reason_text,
                    source_event_id=source_event_id,
                )
            return updated

    # ----- classification audit (requirements §8.6) -----------------

    def _record_classification_locked(
        self,
        *,
        case_id: str,
        supergroup_code: str,
        intent_code: Optional[str],
        classified_by: str,
        classifier_type: str,
        child_case_id: Optional[str] = None,
        model_version: Optional[str] = None,
        reason_text: Optional[str] = None,
        source_event_id: Optional[str] = None,
    ) -> ClassificationEvent:
        """Internal: append a ClassificationEvent. Caller already holds
        ``self._lock`` and has confirmed case_id exists."""
        event = ClassificationEvent(
            case_id=case_id,
            child_case_id=child_case_id,
            supergroup_code=supergroup_code,
            intent_code=intent_code,
            classified_by=classified_by,
            classifier_type=classifier_type,
            model_version=model_version,
            reason_text=reason_text,
            source_event_id=source_event_id,
            taxonomy_version=TAXONOMY_VERSION,
        )
        self._history.setdefault(case_id, []).append(event)
        return event

    def record_classification(
        self,
        case_id: str,
        *,
        supergroup_code: str,
        classified_by: str,
        classifier_type: str,
        intent_code: Optional[str] = None,
        child_case_id: Optional[str] = None,
        model_version: Optional[str] = None,
        reason_text: Optional[str] = None,
        source_event_id: Optional[str] = None,
    ) -> ClassificationEvent:
        """Append one ClassificationEvent row to the audit trail.

        Use this when the classification event happens *out-of-band*
        from a CaseStore.update / lookup_or_create call — e.g. the
        steward writes a corrective row, or a Phase 5 classifier batch
        records its own MODEL events. For inline classifications,
        prefer the ``classified_by`` / ``classifier_type`` kwargs on
        ``update`` / ``lookup_or_create``, which write the event
        atomically with the state mutation.

        Raises ``KeyError`` if the case is unknown.
        """
        with self._lock:
            if case_id not in self._cases:
                raise KeyError(f"Unknown case_id: {case_id}")
            return self._record_classification_locked(
                case_id=case_id,
                supergroup_code=supergroup_code,
                intent_code=intent_code,
                classified_by=classified_by,
                classifier_type=classifier_type,
                child_case_id=child_case_id,
                model_version=model_version,
                reason_text=reason_text,
                source_event_id=source_event_id,
            )

    def get_classification_history(
        self, case_id: str,
    ) -> List[ClassificationEvent]:
        """Return the case's classification history in append order.

        Returns an empty list for cases that have never been classified.
        Returns a shallow copy so callers cannot mutate the in-memory
        store; the events themselves are Pydantic frozen-equivalents
        (extra='forbid') so mutation would error anyway.
        """
        with self._lock:
            return list(self._history.get(case_id, []))

    def list_by_tenant(self, tenant_id: str) -> List["OrderCase"]:
        with self._lock:
            return [c for c in self._cases.values() if c.tenant_id == tenant_id]

    def set_pending_override(
        self, case_id: str, override: Any,
    ) -> "OrderCase":
        """ADR-040 §3.1 — atomically attach a `CasePendingOverride`
        to the case. Raises `KeyError` for unknown case_id and
        `ValueError` when the case already carries a pending
        override (forward-only invariant per §4)."""
        with self._lock:
            existing = self._cases.get(case_id)
            if existing is None:
                raise KeyError(f"Unknown case_id: {case_id}")
            if existing.pending_override is not None:
                raise ValueError(
                    f"Case {case_id} already has a pending_override; "
                    "second initiator must wait for cosign-resolve.",
                )
            # PO #17 — bump updated_at on every mutation.
            updated = existing.model_copy(
                update={
                    "pending_override": override,
                    "status": "OPEN_AWAITING_HUMAN",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            self._cases[case_id] = updated
            return updated

    def clear_pending_override(self, case_id: str) -> "OrderCase":
        """Lift the pending_override off the case. Used by both
        the cosign-approve path (after promotion) and the
        cosign-reject path."""
        with self._lock:
            existing = self._cases.get(case_id)
            if existing is None:
                raise KeyError(f"Unknown case_id: {case_id}")
            # PO #17 — bump updated_at on every mutation.
            updated = existing.model_copy(
                update={
                    "pending_override": None,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            self._cases[case_id] = updated
            return updated

    def clear(self) -> None:
        """Test-helper: reset the in-memory store."""
        with self._lock:
            self._cases.clear()
            self._correlation.clear()


# Module-level singleton — case store uses in-memory backing; the
# DB-backed equivalent ships with the V009/V010 migration in Phase H.7.
case_store = CaseStore()
