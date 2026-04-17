"""Database repository layer.

Provides CRUD operations for exceptions, traces, and policy overrides
backed by either PostgreSQL or SQLite via the connection adapter.

Architecture_v3.md Section 9.2 (schema), Section 11.3 (tenant isolation).

All queries include a ``tenant_id`` predicate for application-layer
tenant isolation. PostgreSQL RLS provides the defense-in-depth layer.
"""

from __future__ import annotations

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

        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """INSERT INTO exceptions
                   (id, tenant_id, order_id, event_type, intent,
                    lifecycle_state, shadow_verdict, selected_recipe,
                    final_status, trace_id, resolution_data,
                    original_event, reanalysis_history,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record_id, tenant_id, order_id, event_type, intent,
                 lifecycle_state, shadow_verdict, selected_recipe,
                 final_status, trace_id, res_data,
                 original_event_json, history_json,
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
                          original_event, reanalysis_history,
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
                           original_event, reanalysis_history,
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
        for json_col in ("resolution_data", "original_event", "reanalysis_history"):
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
                       SUM(CASE WHEN lifecycle_state IN ('INGESTED','CLASSIFYING','AUDITING','EXECUTING') THEN 1 ELSE 0 END) as open_exc,
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
        "original_event", "reanalysis_history",
        "created_at", "updated_at",
    )

    def _to_dict(self, row) -> Dict[str, Any]:
        r = _row_to_dict(row, self._COLUMNS)
        for json_col in ("resolution_data", "original_event", "reanalysis_history"):
            if isinstance(r.get(json_col), str):
                r[json_col] = _json_loads(r[json_col])
        # Pre-V002 rows had these nested under resolution_data reserved keys.
        # Surface them transparently so callers see a uniform shape.
        rd = r.get("resolution_data") if isinstance(r.get("resolution_data"), dict) else None
        if rd is not None:
            if r.get("original_event") is None and "_original_event" in rd:
                r["original_event"] = rd.get("_original_event")
            if not r.get("reanalysis_history") and "_reanalysis_history" in rd:
                r["reanalysis_history"] = rd.get("_reanalysis_history") or []
        # Normalise: always provide a list, never None, for reanalysis_history.
        if r.get("reanalysis_history") is None:
            r["reanalysis_history"] = []
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

class PolicyRepository:
    """CRUD for ``policy_overrides`` and ``policy_audit_log`` tables."""

    def __init__(self, adapter=None):
        self._adapter = adapter or create_adapter()

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

            # Insert audit log entry (SOX requirement)
            audit_id = _uuid()
            cur.execute(
                """INSERT INTO policy_audit_log
                   (id, tenant_id, policy_key, previous_value, new_value,
                    changed_by, change_reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (audit_id, tenant_id, policy_key, previous_value,
                 value_json, created_by, change_reason, now),
            )

        return {
            "id": record_id,
            "tenant_id": tenant_id,
            "policy_key": policy_key,
            "value": value,
            "effective_from": now,
            "created_by": created_by,
        }

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
                          new_value, changed_by, change_reason, created_at
                   FROM policy_audit_log
                   WHERE tenant_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (tenant_id, limit),
            )
            rows = cur.fetchall()
        _AUDIT_COLS = (
            "id", "tenant_id", "policy_key", "previous_value",
            "new_value", "changed_by", "change_reason", "created_at",
        )
        results = []
        for row in rows:
            r = _row_to_dict(row, _AUDIT_COLS)
            if isinstance(r.get("previous_value"), str):
                r["previous_value"] = _json_loads(r["previous_value"])
            if isinstance(r.get("new_value"), str):
                r["new_value"] = _json_loads(r["new_value"])
            results.append(r)
        return results
