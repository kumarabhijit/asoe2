"""Database repository layer.

Provides CRUD operations for exceptions, traces, and policy overrides
backed by either PostgreSQL or SQLite via the connection adapter.

Architecture_v3.md Section 9.2 (schema), Section 11.3 (tenant isolation).

All queries include a ``tenant_id`` predicate for application-layer
tenant isolation. PostgreSQL RLS provides the defense-in-depth layer.

Phase 4: ``policy_audit_log`` is hash-chained — every insert reads the
prior row's ``event_hash`` and computes ``event_hash = sha256(prev || json)``.
A DB-level trigger (V003 migration) additionally rejects UPDATE and DELETE,
so a casual ``DELETE FROM policy_audit_log`` is a hard error. The companion
``verify_audit_chain()`` walks the chain and reports the first broken
event. Same hash function as ``api/store.py`` (in-memory store) — both
sides stay in lockstep.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from contracts.models import STATUS_TO_LIFECYCLE
from db.connection import SQLiteAdapter, create_adapter

logger = logging.getLogger("asoe.db.repository")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return str(uuid4())


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, default=str)


def _json_loads(s: Optional[str]) -> Any:
    if s is None:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return s


def _row_to_dict(row, columns: tuple[str, ...]) -> Dict[str, Any]:
    """Convert a database row to a dict using column names.

    Works with both dict-like rows (psycopg2 RealDictCursor, sqlite3.Row)
    and plain tuples (positional indexing fallback).
    """
    if hasattr(row, "keys"):
        return dict(row)
    return {col: row[i] for i, col in enumerate(columns)}


# ---------------------------------------------------------------------------
# Exception Repository
# ---------------------------------------------------------------------------

class ExceptionRepository:
    """CRUD for the ``exceptions`` table."""

    def __init__(self, adapter=None):
        self._adapter = adapter or create_adapter()

    def create(
        self,
        tenant_id: str,
        order_id: str,
        event_type: str,
        trace_id: str,
        intent: Optional[str] = None,
        lifecycle_state: Optional[str] = None,
        shadow_verdict: Optional[str] = None,
        selected_recipe: Optional[str] = None,
        final_status: Optional[str] = None,
        resolution_data: Optional[Dict[str, Any]] = None,
        original_event: Optional[Dict[str, Any]] = None,
        reanalysis_history: Optional[List[Dict[str, Any]]] = None,
        enrichment_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        record_id = _uuid()
        now = _now()
        if not lifecycle_state:
            lifecycle_state = STATUS_TO_LIFECYCLE.get(final_status or "", "INGESTED")
        res_data = _json_dumps(resolution_data or {})
        original_event_json = (
            _json_dumps(original_event) if original_event is not None else None
        )
        history_json = _json_dumps(reanalysis_history or [])
        enrichment_json = _json_dumps(enrichment_context or {})

        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """INSERT INTO exceptions
                   (id, tenant_id, order_id, event_type, intent,
                    lifecycle_state, shadow_verdict, selected_recipe,
                    final_status, trace_id, resolution_data,
                    original_event, reanalysis_history, enrichment_context,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record_id, tenant_id, order_id, event_type, intent,
                 lifecycle_state, shadow_verdict, selected_recipe,
                 final_status, trace_id, res_data,
                 original_event_json, history_json, enrichment_json,
                 now, now),
            )

        return {
            "id": record_id,
            "tenant_id": tenant_id,
            "order_id": order_id,
            "event_type": event_type,
            "intent": intent,
            "lifecycle_state": lifecycle_state,
            "shadow_verdict": shadow_verdict,
            "selected_recipe": selected_recipe,
            "final_status": final_status,
            "trace_id": trace_id,
            "resolution_data": resolution_data or {},
            "original_event": original_event,
            "reanalysis_history": reanalysis_history or [],
            "enrichment_context": enrichment_context or {},
            "resolved_by": None,
            "resolved_action": None,
            "resolution_notes": None,
            "created_at": now,
            "updated_at": now,
        }

    def get(self, exception_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """SELECT id, tenant_id, order_id, event_type, intent,
                          lifecycle_state, shadow_verdict, selected_recipe,
                          final_status, trace_id, resolution_data,
                          resolved_by, resolved_action, resolution_notes,
                          original_event, reanalysis_history, enrichment_context,
                          created_at, updated_at
                   FROM exceptions
                   WHERE id = ? AND tenant_id = ?""",
                (exception_id, tenant_id),
            )
            row = cur.fetchone()
        if not row:
            return None
        return self._to_dict(row)

    def list(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        intent: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str], bool]:
        conditions = ["tenant_id = ?"]
        params: list = [tenant_id]

        if status:
            conditions.append("lifecycle_state = ?")
            params.append(status)
        if intent:
            conditions.append("intent = ?")
            params.append(intent)

        # Cursor-based pagination: records created before the cursor record
        if cursor:
            conditions.append("""created_at <= (
                SELECT created_at FROM exceptions WHERE id = ?
            ) AND id != ?""")
            params.extend([cursor, cursor])

        where = " AND ".join(conditions)
        # Fetch one extra to determine has_more
        query = f"""SELECT id, tenant_id, order_id, event_type, intent,
                           lifecycle_state, shadow_verdict, selected_recipe,
                           final_status, trace_id, resolution_data,
                           resolved_by, resolved_action, resolution_notes,
                           original_event, reanalysis_history, enrichment_context,
                           created_at, updated_at
                    FROM exceptions
                    WHERE {where}
                    ORDER BY created_at DESC
                    LIMIT ?"""
        params.append(limit + 1)

        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()

        records = [self._to_dict(r) for r in rows[:limit]]
        has_more = len(rows) > limit
        next_cursor = records[-1]["id"] if has_more and records else None
        return records, next_cursor, has_more

    def update(
        self, exception_id: str, tenant_id: str, **fields
    ) -> Optional[Dict[str, Any]]:
        if not fields:
            return self.get(exception_id, tenant_id)

        # Serialize JSON columns before binding. Callers pass native Python
        # objects for these fields; we persist them as JSON strings.
        for json_col in (
            "resolution_data", "original_event",
            "reanalysis_history", "enrichment_context",
        ):
            if json_col in fields and not isinstance(fields[json_col], str):
                fields[json_col] = _json_dumps(fields[json_col])

        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values())
        values.extend([exception_id, tenant_id])

        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                f"UPDATE exceptions SET {set_clause} WHERE id = ? AND tenant_id = ?",
                tuple(values),
            )

        return self.get(exception_id, tenant_id)

    def stats(self, tenant_id: str) -> Dict[str, Any]:
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """SELECT
                       COUNT(*) as total,
                       SUM(CASE WHEN lifecycle_state IN ('INGESTED','CLASSIFYING','AUDITING') THEN 1 ELSE 0 END) as open_exc,
                       SUM(CASE WHEN lifecycle_state = 'RESOLVED' THEN 1 ELSE 0 END) as auto_resolved,
                       SUM(CASE WHEN lifecycle_state = 'PENDING_REVIEW' THEN 1 ELSE 0 END) as manual_review,
                       SUM(CASE WHEN lifecycle_state = 'BLOCKED' THEN 1 ELSE 0 END) as blocked,
                       SUM(CASE WHEN lifecycle_state = 'FAILED' THEN 1 ELSE 0 END) as failed
                   FROM exceptions
                   WHERE tenant_id = ?""",
                (tenant_id,),
            )
            row = cur.fetchone()

        # Aggregate by intent
        by_intent: Dict[str, int] = {}
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                "SELECT COALESCE(intent, 'UNKNOWN') as intent_key, COUNT(*) as cnt FROM exceptions WHERE tenant_id = ? GROUP BY intent_key",
                (tenant_id,),
            )
            for r in cur.fetchall():
                k = r[0] if not hasattr(r, "keys") else r["intent_key"]
                v = r[1] if not hasattr(r, "keys") else r["cnt"]
                by_intent[k] = v

        # Aggregate by lifecycle_state
        by_lifecycle_state: Dict[str, int] = {}
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                "SELECT lifecycle_state, COUNT(*) as cnt FROM exceptions WHERE tenant_id = ? GROUP BY lifecycle_state",
                (tenant_id,),
            )
            for r in cur.fetchall():
                k = r[0] if not hasattr(r, "keys") else r["lifecycle_state"]
                v = r[1] if not hasattr(r, "keys") else r["cnt"]
                by_lifecycle_state[k] = v

        # Aggregate by shadow_verdict
        by_shadow_verdict: Dict[str, int] = {}
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                "SELECT shadow_verdict, COUNT(*) as cnt FROM exceptions WHERE tenant_id = ? AND shadow_verdict IS NOT NULL GROUP BY shadow_verdict",
                (tenant_id,),
            )
            for r in cur.fetchall():
                k = r[0] if not hasattr(r, "keys") else r["shadow_verdict"]
                v = r[1] if not hasattr(r, "keys") else r["cnt"]
                by_shadow_verdict[k] = v

        return {
            "total_exceptions": row[0] or 0,
            "open_exceptions": row[1] or 0,
            "auto_resolved": row[2] or 0,
            "manual_review": row[3] or 0,
            "blocked": row[4] or 0,
            "failed": row[5] or 0,
            "avg_resolution_time_seconds": None,
            "by_intent": by_intent,
            "by_lifecycle_state": by_lifecycle_state,
            "by_shadow_verdict": by_shadow_verdict,
        }

    _COLUMNS = (
        "id", "tenant_id", "order_id", "event_type", "intent",
        "lifecycle_state", "shadow_verdict", "selected_recipe",
        "final_status", "trace_id", "resolution_data", "resolved_by",
        "resolved_action", "resolution_notes",
        "original_event", "reanalysis_history", "enrichment_context",
        "created_at", "updated_at",
    )

    def _to_dict(self, row) -> Dict[str, Any]:
        r = _row_to_dict(row, self._COLUMNS)
        for json_col in (
            "resolution_data", "original_event",
            "reanalysis_history", "enrichment_context",
        ):
            if isinstance(r.get(json_col), str):
                r[json_col] = _json_loads(r[json_col])
        # Normalise: always provide a list, never None, for reanalysis_history.
        if r.get("reanalysis_history") is None:
            r["reanalysis_history"] = []
        # Normalise: always provide a dict, never None, for enrichment_context
        # (V004 default '{}' guarantees this for new rows; older callers may
        # have inserted via the in-memory bridge with no value).
        if r.get("enrichment_context") is None:
            r["enrichment_context"] = {}
        # Postgres returns UUID columns as ``uuid.UUID`` objects and
        # TIMESTAMPTZ columns as ``datetime`` objects. The downstream
        # ExceptionSummary / ExceptionDetailResponse pydantic models
        # declare these as ``str``, so unconverted values trigger
        # ValidationError(type=string_type) and the API 500s on
        # GET /api/v1/exceptions. SQLite returns plain strings already,
        # so the isinstance guards are no-ops there.
        for uuid_col in ("id", "trace_id"):
            v = r.get(uuid_col)
            if v is not None and not isinstance(v, str):
                r[uuid_col] = str(v)
        for ts_col in ("created_at", "updated_at"):
            v = r.get(ts_col)
            if v is not None and not isinstance(v, str):
                # datetime → ISO 8601; anything else stringifies safely.
                r[ts_col] = v.isoformat() if hasattr(v, "isoformat") else str(v)
        return r


# ---------------------------------------------------------------------------
# Trace Repository
# ---------------------------------------------------------------------------

class TraceRepository:
    """CRUD for the ``traces`` table."""

    def __init__(self, adapter=None):
        self._adapter = adapter or create_adapter()

    def create(
        self,
        exception_id: str,
        trace_id: str,
        tenant_id: str,
        trace_record: Dict[str, Any],
    ) -> Dict[str, Any]:
        record_id = _uuid()
        now = _now()
        trace_json = _json_dumps(trace_record)

        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """INSERT INTO traces
                   (id, exception_id, trace_id, tenant_id, trace_record, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (record_id, exception_id, trace_id, tenant_id, trace_json, now),
            )

        return {
            "id": record_id,
            "exception_id": exception_id,
            "trace_id": trace_id,
            "tenant_id": tenant_id,
            "trace_record": trace_record,
            "created_at": now,
        }

    def get_by_exception(
        self, exception_id: str, tenant_id: str
    ) -> Optional[Dict[str, Any]]:
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """SELECT id, exception_id, trace_id, tenant_id,
                          trace_record, created_at
                   FROM traces
                   WHERE exception_id = ? AND tenant_id = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (exception_id, tenant_id),
            )
            row = cur.fetchone()
        if not row:
            return None
        _TRACE_COLS = ("id", "exception_id", "trace_id", "tenant_id", "trace_record", "created_at")
        r = _row_to_dict(row, _TRACE_COLS)
        if isinstance(r.get("trace_record"), str):
            r["trace_record"] = _json_loads(r["trace_record"])
        return r


# ---------------------------------------------------------------------------
# Policy Repository
# ---------------------------------------------------------------------------

def _audit_event_hash(prev_hash: str, fields: Dict[str, Any]) -> str:
    """Canonical-JSON SHA-256 over the event payload + prev_hash.

    Mirrors api/store.py::log_audit_event AND db/migrations/runner.py
    backfill — all three implementations must produce identical hashes
    for the same event so a chain written by one is verifiable by the
    others.
    """
    payload = json.dumps(
        {k: fields[k] for k in sorted(fields)},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(
        (prev_hash + "|" + payload).encode("utf-8")
    ).hexdigest()


class PolicyRepository:
    """CRUD for ``policy_overrides`` and ``policy_audit_log`` tables."""

    def __init__(self, adapter=None):
        self._adapter = adapter or create_adapter()

    def _last_event_hash(self, cur, tenant_id: str) -> str:
        """Per-tenant prev_hash lookup. Returns 'GENESIS' for first row."""
        cur.execute(
            "SELECT event_hash FROM policy_audit_log "
            "WHERE tenant_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (tenant_id,),
        )
        row = cur.fetchone()
        return row[0] if row else "GENESIS"

    def _insert_audit_event(
        self,
        cur,
        *,
        tenant_id: str,
        policy_key: str,
        previous_value: Optional[str],
        new_value: str,
        changed_by: str,
        change_reason: Optional[str],
    ) -> str:
        """Hash-chained INSERT into policy_audit_log. Returns the new row id."""
        audit_id = _uuid()
        now = _now()
        prev_hash = self._last_event_hash(cur, tenant_id)
        event_hash = _audit_event_hash(
            prev_hash,
            {
                "id": audit_id,
                "tenant_id": tenant_id,
                "policy_key": policy_key,
                "previous_value": previous_value,
                "new_value": new_value,
                "changed_by": changed_by,
                "change_reason": change_reason,
                "created_at": now,
                "prev_hash": prev_hash,
            },
        )
        cur.execute(
            """INSERT INTO policy_audit_log
               (id, tenant_id, policy_key, previous_value, new_value,
                changed_by, change_reason, created_at,
                prev_hash, event_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (audit_id, tenant_id, policy_key, previous_value, new_value,
             changed_by, change_reason, now, prev_hash, event_hash),
        )
        return audit_id

    def create_override(
        self,
        tenant_id: str,
        policy_key: str,
        value: Any,
        created_by: str,
        change_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        record_id = _uuid()
        now = _now()
        value_json = _json_dumps(value)

        # Look up previous value for audit log
        previous = self.get_override(tenant_id, policy_key)
        previous_value = _json_dumps(previous["value"]) if previous else None

        with self._adapter.cursor(tenant_id) as cur:
            # Insert new override
            cur.execute(
                """INSERT INTO policy_overrides
                   (id, tenant_id, policy_key, value, effective_from,
                    created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (record_id, tenant_id, policy_key, value_json, now,
                 created_by, now),
            )

            # Insert hash-chained audit log entry (SOX + tamper-evidence).
            self._insert_audit_event(
                cur,
                tenant_id=tenant_id,
                policy_key=policy_key,
                previous_value=previous_value,
                new_value=value_json,
                changed_by=created_by,
                change_reason=change_reason,
            )

        return {
            "id": record_id,
            "tenant_id": tenant_id,
            "policy_key": policy_key,
            "value": value,
            "effective_from": now,
            "created_by": created_by,
        }

    def create_audit_event(
        self,
        tenant_id: str,
        policy_key: str,
        previous_value: Any,
        new_value: Any,
        changed_by: str,
        change_reason: Optional[str] = None,
    ) -> str:
        """Audit-only insert (no policy_override row).

        Used by the exception-store DB backend for events like
        EXCEPTION_RESOLVED / EXCEPTION_OVERRIDE_INITIATED — these are
        application-level audit events, not policy threshold tunings.
        Returns the new audit row id.
        """
        with self._adapter.cursor(tenant_id) as cur:
            return self._insert_audit_event(
                cur,
                tenant_id=tenant_id,
                policy_key=policy_key,
                previous_value=_json_dumps(previous_value)
                    if previous_value is not None else None,
                new_value=_json_dumps(new_value),
                changed_by=changed_by,
                change_reason=change_reason,
            )

    def verify_audit_chain(
        self, tenant_id: str
    ) -> tuple[bool, Optional[int]]:
        """Walk the tenant's chain in (created_at, id) order.

        Returns (True, None) on a valid chain, or (False, idx) where idx
        is the zero-based position of the first event whose stored
        ``event_hash`` does not match a recompute, or whose ``prev_hash``
        does not link to the predecessor's ``event_hash``.
        """
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """SELECT id, tenant_id, policy_key, previous_value,
                          new_value, changed_by, change_reason, created_at,
                          prev_hash, event_hash
                     FROM policy_audit_log
                    WHERE tenant_id = ?
                    ORDER BY created_at, id""",
                (tenant_id,),
            )
            rows = cur.fetchall()
        expected_prev = "GENESIS"
        for i, r in enumerate(rows):
            (audit_id, t_id, policy_key, previous_value, new_value,
             changed_by, change_reason, created_at,
             prev_hash, event_hash) = r
            if prev_hash != expected_prev:
                return False, i
            recomputed = _audit_event_hash(
                prev_hash,
                {
                    "id": audit_id,
                    "tenant_id": t_id,
                    "policy_key": policy_key,
                    "previous_value": previous_value,
                    "new_value": new_value,
                    "changed_by": changed_by,
                    "change_reason": change_reason,
                    "created_at": created_at,
                    "prev_hash": prev_hash,
                },
            )
            if recomputed != event_hash:
                return False, i
            expected_prev = event_hash
        return True, None

    def get_override(
        self, tenant_id: str, policy_key: str
    ) -> Optional[Dict[str, Any]]:
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """SELECT id, tenant_id, policy_key, value,
                          effective_from, effective_until, created_by, created_at
                   FROM policy_overrides
                   WHERE tenant_id = ? AND policy_key = ?
                   ORDER BY effective_from DESC LIMIT 1""",
                (tenant_id, policy_key),
            )
            row = cur.fetchone()
        if not row:
            return None
        _OVERRIDE_COLS = (
            "id", "tenant_id", "policy_key", "value",
            "effective_from", "effective_until", "created_by", "created_at",
        )
        r = _row_to_dict(row, _OVERRIDE_COLS)
        if isinstance(r.get("value"), str):
            r["value"] = _json_loads(r["value"])
        return r

    def list_audit_log(
        self, tenant_id: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """SELECT id, tenant_id, policy_key, previous_value,
                          new_value, changed_by, change_reason, created_at,
                          prev_hash, event_hash
                   FROM policy_audit_log
                   WHERE tenant_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (tenant_id, limit),
            )
            rows = cur.fetchall()
        _AUDIT_COLS = (
            "id", "tenant_id", "policy_key", "previous_value",
            "new_value", "changed_by", "change_reason", "created_at",
            "prev_hash", "event_hash",
        )
        results = []
        for row in rows:
            r = _row_to_dict(row, _AUDIT_COLS)
            if isinstance(r.get("previous_value"), str):
                try:
                    r["previous_value"] = _json_loads(r["previous_value"])
                except Exception:
                    pass  # leave as raw string (matches in-memory store)
            if isinstance(r.get("new_value"), str):
                try:
                    r["new_value"] = _json_loads(r["new_value"])
                except Exception:
                    pass
            results.append(r)
        return results
