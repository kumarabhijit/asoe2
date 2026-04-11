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
    ) -> Dict[str, Any]:
        record_id = _uuid()
        now = _now()
        if not lifecycle_state:
            lifecycle_state = STATUS_TO_LIFECYCLE.get(final_status or "", "INGESTED")
        res_data = _json_dumps(resolution_data or {})

        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """INSERT INTO exceptions
                   (id, tenant_id, order_id, event_type, intent,
                    lifecycle_state, shadow_verdict, selected_recipe,
                    final_status, trace_id, resolution_data,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record_id, tenant_id, order_id, event_type, intent,
                 lifecycle_state, shadow_verdict, selected_recipe,
                 final_status, trace_id, res_data,
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
                          created_at, updated_at
                   FROM exceptions
                   WHERE id = ? AND tenant_id = ?""",
                (exception_id, tenant_id),
            )
            row = cur.fetchone()
        if not row:
            return None
        return self._row_to_dict(row)

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
                           created_at, updated_at
                    FROM exceptions
                    WHERE {where}
                    ORDER BY created_at DESC
                    LIMIT ?"""
        params.append(limit + 1)

        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()

        records = [self._row_to_dict(r) for r in rows[:limit]]
        has_more = len(rows) > limit
        next_cursor = records[-1]["id"] if has_more and records else None
        return records, next_cursor, has_more

    def update(
        self, exception_id: str, tenant_id: str, **fields
    ) -> Optional[Dict[str, Any]]:
        if not fields:
            return self.get(exception_id, tenant_id)

        # Serialize resolution_data if present
        if "resolution_data" in fields and not isinstance(fields["resolution_data"], str):
            fields["resolution_data"] = _json_dumps(fields["resolution_data"])

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

    def stats(self, tenant_id: str) -> Dict[str, int]:
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """SELECT
                       COUNT(*) as total,
                       SUM(CASE WHEN lifecycle_state IN ('INGESTED','CLASSIFYING','AUDITING','EXECUTING') THEN 1 ELSE 0 END) as open,
                       SUM(CASE WHEN lifecycle_state = 'RESOLVED' THEN 1 ELSE 0 END) as auto_resolved,
                       SUM(CASE WHEN lifecycle_state = 'PENDING_REVIEW' THEN 1 ELSE 0 END) as manual_review,
                       SUM(CASE WHEN lifecycle_state = 'BLOCKED' THEN 1 ELSE 0 END) as blocked,
                       SUM(CASE WHEN lifecycle_state = 'FAILED' THEN 1 ELSE 0 END) as failed
                   FROM exceptions
                   WHERE tenant_id = ?""",
                (tenant_id,),
            )
            row = cur.fetchone()
        return {
            "total": row[0] or 0,
            "open": row[1] or 0,
            "auto_resolved": row[2] or 0,
            "manual_review": row[3] or 0,
            "blocked": row[4] or 0,
            "failed": row[5] or 0,
        }

    def _row_to_dict(self, row) -> Dict[str, Any]:
        r = dict(row) if hasattr(row, "keys") else {
            "id": row[0], "tenant_id": row[1], "order_id": row[2],
            "event_type": row[3], "intent": row[4], "lifecycle_state": row[5],
            "shadow_verdict": row[6], "selected_recipe": row[7],
            "final_status": row[8], "trace_id": row[9],
            "resolution_data": row[10], "resolved_by": row[11],
            "resolved_action": row[12], "resolution_notes": row[13],
            "created_at": row[14], "updated_at": row[15],
        }
        # Deserialize JSON fields
        if isinstance(r.get("resolution_data"), str):
            r["resolution_data"] = _json_loads(r["resolution_data"])
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
        r = dict(row) if hasattr(row, "keys") else {
            "id": row[0], "exception_id": row[1], "trace_id": row[2],
            "tenant_id": row[3], "trace_record": row[4], "created_at": row[5],
        }
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
        r = dict(row) if hasattr(row, "keys") else {
            "id": row[0], "tenant_id": row[1], "policy_key": row[2],
            "value": row[3], "effective_from": row[4],
            "effective_until": row[5], "created_by": row[6],
            "created_at": row[7],
        }
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
        results = []
        for row in rows:
            r = dict(row) if hasattr(row, "keys") else {
                "id": row[0], "tenant_id": row[1], "policy_key": row[2],
                "previous_value": row[3], "new_value": row[4],
                "changed_by": row[5], "change_reason": row[6],
                "created_at": row[7],
            }
            if isinstance(r.get("previous_value"), str):
                r["previous_value"] = _json_loads(r["previous_value"])
            if isinstance(r.get("new_value"), str):
                r["new_value"] = _json_loads(r["new_value"])
            results.append(r)
        return results
