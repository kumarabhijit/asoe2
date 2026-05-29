"""Database repository layer.

Provides CRUD operations for exceptions, traces, policy overrides, and
tenant_config (ADR-030 layers 2-5) backed by either PostgreSQL or
SQLite via the connection adapter.

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

A9 (PR-C.1): ``TenantConfigRepository`` persists layers 2-5 of the
DUPLICATE_PO score-weight hierarchy. Layer 1 (platform) lives on disk
in gateways/configs/duplicate_po/defaults.json. Edit history flows
through policy_audit_log via PolicyRepository.create_audit_event so
the SOX surface remains a single hash-chained log.

A9 (PR-C.2): ``PolicyRepository.list_audit_log`` accepts an optional
``policy_key_prefix`` so the GET /api/v1/config/tenants/.../audit
endpoint can scope to ConfigChange entries (prefix
``duplicate_po.weights.``) without a Python-side filter pass.
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
        # V018 — Case & Intent Super-Group fields (requirements §5).
        # Optional in Phase 2 so existing callers stay green; Phase 3
        # classifier wiring starts passing them.
        supergroup_code: Optional[str] = None,
        intent_code: Optional[str] = None,
        divergence_reason: Optional[str] = None,
        sap_block_field: Optional[str] = None,
        scope: Optional[str] = None,
        parent_case_id: Optional[str] = None,
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
                    supergroup_code, intent_code, divergence_reason,
                    sap_block_field, scope,
                    created_at, updated_at, parent_case_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record_id, tenant_id, order_id, event_type, intent,
                 lifecycle_state, shadow_verdict, selected_recipe,
                 final_status, trace_id, res_data,
                 original_event_json, history_json, enrichment_json,
                 supergroup_code, intent_code, divergence_reason,
                 sap_block_field, scope,
                 now, now, parent_case_id),
            )

        return {
            "id": record_id, "tenant_id": tenant_id, "order_id": order_id,
            "event_type": event_type, "intent": intent,
            "lifecycle_state": lifecycle_state, "shadow_verdict": shadow_verdict,
            "selected_recipe": selected_recipe, "final_status": final_status,
            "trace_id": trace_id, "resolution_data": resolution_data or {},
            "original_event": original_event,
            "reanalysis_history": reanalysis_history or [],
            "enrichment_context": enrichment_context or {},
            "resolved_by": None, "resolved_action": None, "resolution_notes": None,
            "supergroup_code": supergroup_code, "intent_code": intent_code,
            "divergence_reason": divergence_reason,
            "sap_block_field": sap_block_field, "scope": scope,
            "created_at": now, "updated_at": now,
            "parent_case_id": parent_case_id,
        }

    def get(
        self, exception_id: str, tenant_id: str,
        *, _cursor: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        """Fetch one exception row. Pass ``_cursor`` to share the
        transaction with surrounding writes (see ``update`` docstring)."""
        sql = """SELECT id, tenant_id, order_id, event_type, intent,
                        lifecycle_state, shadow_verdict, selected_recipe,
                        final_status, trace_id, resolution_data,
                        resolved_by, resolved_action, resolution_notes,
                        original_event, reanalysis_history, enrichment_context,
                        supergroup_code, intent_code, divergence_reason,
                        sap_block_field, scope,
                        created_at, updated_at, parent_case_id
                 FROM exceptions
                 WHERE id = ? AND tenant_id = ?"""
        if _cursor is not None:
            _cursor.execute(sql, (exception_id, tenant_id))
            row = _cursor.fetchone()
        else:
            with self._adapter.cursor(tenant_id) as cur:
                cur.execute(sql, (exception_id, tenant_id))
                row = cur.fetchone()
        if not row:
            return None
        return self._to_dict(row)

    def list(
        self, tenant_id: str, status: Optional[str] = None,
        intent: Optional[str] = None, limit: int = 50,
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
        if cursor:
            conditions.append("""created_at <= (
                SELECT created_at FROM exceptions WHERE id = ?
            ) AND id != ?""")
            params.extend([cursor, cursor])
        where = " AND ".join(conditions)
        query = f"""SELECT id, tenant_id, order_id, event_type, intent,
                           lifecycle_state, shadow_verdict, selected_recipe,
                           final_status, trace_id, resolution_data,
                           resolved_by, resolved_action, resolution_notes,
                           original_event, reanalysis_history, enrichment_context,
                           supergroup_code, intent_code, divergence_reason,
                           sap_block_field, scope,
                           created_at, updated_at, parent_case_id
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
        self, exception_id: str, tenant_id: str,
        *, _cursor: Optional[Any] = None, **fields,
    ) -> Optional[Dict[str, Any]]:
        """Update fields on an exception row.

        Pass ``_cursor`` when the caller wants this UPDATE to share a
        transaction with subsequent writes (e.g. DatabaseBackedStore.update
        wraps UPDATE + classification-history INSERT in one cursor so a
        failed audit write rolls back the row mutation — review finding #2
        from the Phase-5/6 review checkpoint).
        """
        if not fields:
            return self.get(exception_id, tenant_id, _cursor=_cursor)
        for json_col in ("resolution_data", "original_event",
                         "reanalysis_history", "enrichment_context"):
            if json_col in fields and not isinstance(fields[json_col], str):
                fields[json_col] = _json_dumps(fields[json_col])
        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values())
        values.extend([exception_id, tenant_id])
        sql = (
            f"UPDATE exceptions SET {set_clause} WHERE id = ? AND tenant_id = ?"
        )
        if _cursor is not None:
            _cursor.execute(sql, tuple(values))
        else:
            with self._adapter.cursor(tenant_id) as cur:
                cur.execute(sql, tuple(values))
        return self.get(exception_id, tenant_id, _cursor=_cursor)

    def list_orphans(self, tenant_id: str) -> List[Dict[str, Any]]:
        """All exceptions for a tenant with ``parent_case_id IS NULL`` —
        the Phase H.7 backfill input set. Tenant-scoped so RLS / app-layer
        isolation holds; the ops runner iterates per tenant."""
        sql = (
            f"SELECT {', '.join(self._COLUMNS)} FROM exceptions "
            f"WHERE tenant_id = ? AND parent_case_id IS NULL "
            f"ORDER BY created_at"
        )
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(sql, (tenant_id,))
            return [self._to_dict(r) for r in cur.fetchall()]

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
        # V018 — Case & Intent Super-Group columns (requirements §5).
        # Nullable in Phase 2; populated by the Phase 3 classifier wiring.
        "supergroup_code", "intent_code", "divergence_reason",
        "sap_block_field", "scope",
        "created_at", "updated_at",
        # V009 (corrected) — child→parent case edge. Phase H.7 persists it
        # so the link survives a restart on the DB-backed path.
        "parent_case_id",
    )

    def _to_dict(self, row) -> Dict[str, Any]:
        r = _row_to_dict(row, self._COLUMNS)
        for json_col in ("resolution_data", "original_event",
                         "reanalysis_history", "enrichment_context"):
            if isinstance(r.get(json_col), str):
                r[json_col] = _json_loads(r[json_col])
        if r.get("reanalysis_history") is None:
            r["reanalysis_history"] = []
        if r.get("enrichment_context") is None:
            r["enrichment_context"] = {}
        for uuid_col in ("id", "trace_id"):
            v = r.get(uuid_col)
            if v is not None and not isinstance(v, str):
                r[uuid_col] = str(v)
        for ts_col in ("created_at", "updated_at"):
            v = r.get(ts_col)
            if v is not None and not isinstance(v, str):
                r[ts_col] = v.isoformat() if hasattr(v, "isoformat") else str(v)
        return r


# ---------------------------------------------------------------------------
# Trace Repository
# ---------------------------------------------------------------------------

class TraceRepository:
    """CRUD for the ``traces`` table."""

    def __init__(self, adapter=None):
        self._adapter = adapter or create_adapter()

    def create(self, exception_id: str, trace_id: str, tenant_id: str,
               trace_record: Dict[str, Any]) -> Dict[str, Any]:
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
            "id": record_id, "exception_id": exception_id,
            "trace_id": trace_id, "tenant_id": tenant_id,
            "trace_record": trace_record, "created_at": now,
        }

    def get_by_exception(self, exception_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
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
# Classification History Repository (V020)
# ---------------------------------------------------------------------------


class ClassificationHistoryRepository:
    """Append-only writer for ``case_classification_history`` (V020).

    The table's UPDATE/DELETE/TRUNCATE/INSERT-OR-REPLACE triggers reject
    every mutation path; this repository only writes INSERTs. Reads
    surface the audit trail for the
    ``GET /api/v1/cases/{id}/classification-history`` endpoint when the
    DB-backed store is active.

    Requirements: docs/specs/case-intent-supergroup-requirements.md §8.6,
    acceptance criterion #9.
    """

    _COLUMNS = (
        "id", "tenant_id", "case_id", "child_case_id", "supergroup_code",
        "intent_code", "classified_at", "classified_by", "classifier_type",
        "model_version", "reason_text", "source_event_id",
        "taxonomy_version",
    )

    def __init__(self, adapter=None):
        self._adapter = adapter or create_adapter()

    def create(
        self,
        *,
        tenant_id: str,
        case_id: str,
        supergroup_code: str,
        classified_by: str,
        classifier_type: str,
        intent_code: Optional[str] = None,
        child_case_id: Optional[str] = None,
        model_version: Optional[str] = None,
        reason_text: Optional[str] = None,
        source_event_id: Optional[str] = None,
        taxonomy_version: str = "",
        _cursor: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Append one classification event. ``taxonomy_version`` must be
        passed by the caller (read from the active YAML at app boot).
        ``tenant_id`` is required (ADR-028 Guard-rail 4 — every audit row
        carries tenant_id for cross-tenant query filtering).
        Returns the inserted row as a dict for the call-site to log."""
        if not tenant_id:
            raise ValueError("tenant_id is required (ADR-028 Guard-rail 4).")
        if not supergroup_code:
            raise ValueError(
                "supergroup_code is required on a classification event."
            )
        if classifier_type not in ("HUMAN", "MODEL", "RULE"):
            raise ValueError(
                f"classifier_type must be HUMAN | MODEL | RULE, got "
                f"{classifier_type!r}"
            )
        if not taxonomy_version:
            raise ValueError(
                "taxonomy_version must be non-empty (read from the active "
                "YAML at app boot)."
            )
        row_id = _uuid()
        now = _now()
        sql = """
            INSERT INTO case_classification_history (
                id, tenant_id, case_id, child_case_id, supergroup_code,
                intent_code, classified_at, classified_by,
                classifier_type, model_version, reason_text,
                source_event_id, taxonomy_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            row_id, tenant_id, case_id, child_case_id, supergroup_code,
            intent_code, now, classified_by, classifier_type,
            model_version, reason_text, source_event_id,
            taxonomy_version,
        )
        if _cursor is not None:
            _cursor.execute(sql, params)
        else:
            with self._adapter.cursor(tenant_id) as cur:
                cur.execute(sql, params)
        return {
            "id": row_id, "tenant_id": tenant_id, "case_id": case_id,
            "child_case_id": child_case_id, "supergroup_code": supergroup_code,
            "intent_code": intent_code, "classified_at": now,
            "classified_by": classified_by, "classifier_type": classifier_type,
            "model_version": model_version, "reason_text": reason_text,
            "source_event_id": source_event_id,
            "taxonomy_version": taxonomy_version,
        }

    def list_by_case(
        self, case_id: str, tenant_id: str,
    ) -> List[Dict[str, Any]]:
        """Return all history rows for a case in append order, scoped
        to ``tenant_id`` (ADR-028 cross-tenant filter)."""
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                f"""
                SELECT {", ".join(self._COLUMNS)}
                FROM case_classification_history
                WHERE tenant_id = ? AND case_id = ?
                ORDER BY classified_at, id
                """,
                (tenant_id, case_id),
            )
            rows = [_row_to_dict(r, self._COLUMNS) for r in cur.fetchall()]
        # child_case_id is a UUID column (V020 FK → exceptions.id); Postgres
        # returns a uuid.UUID object. Coerce to str so the Python contract
        # (ClassificationEvent.child_case_id: Optional[str]) holds on both
        # backends.
        for r in rows:
            v = r.get("child_case_id")
            if v is not None and not isinstance(v, str):
                r["child_case_id"] = str(v)
        return rows


# ---------------------------------------------------------------------------
# OrderCase Repository (Phase H.7 — durable case persistence)
# ---------------------------------------------------------------------------

class OrderCaseRepository:
    """CRUD for the ``order_case`` table (V009/V014/V018/V019/V021).

    Mirrors the in-memory ``CaseStore`` field surface so cases survive a
    container restart on the DB-backed (Azure) path. ``pending_override``
    is stored as a single JSON column (V021); ``will_miss_rdd`` round-trips
    bool↔int across SQLite/Postgres. All write methods accept ``_cursor``
    so the DB-backed store can run find/create/update in one transaction
    (the lookup-or-create and forward-only-override atomicity guarantees).
    """

    _COLUMNS = (
        "case_id", "tenant_id", "customer_id", "origin", "source_channel",
        "supergroup_code", "predecessor_case_id", "will_miss_rdd",
        "customer_po_number", "sales_order_id", "edi_transaction_id",
        "source_email_id", "opened_at", "closed_at", "updated_at", "status",
        "sla_deadline", "sla_due_at", "tier", "working_memory_summary",
        "last_compaction_at", "bundle_version_at_open", "pending_override",
    )

    def __init__(self, adapter=None):
        self._adapter = adapter or create_adapter()

    @staticmethod
    def _encode_override(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return _json_dumps(value)

    def _to_dict(self, row) -> Dict[str, Any]:
        r = _row_to_dict(row, self._COLUMNS)
        # pending_override: JSONB on Postgres may already be a dict; TEXT
        # on SQLite arrives as a JSON string.
        if isinstance(r.get("pending_override"), str):
            r["pending_override"] = _json_loads(r["pending_override"])
        # will_miss_rdd: Postgres returns bool, SQLite returns 0/1.
        if r.get("will_miss_rdd") is not None:
            r["will_miss_rdd"] = bool(r["will_miss_rdd"])
        for ts_col in ("opened_at", "closed_at", "updated_at",
                       "sla_deadline", "sla_due_at"):
            v = r.get(ts_col)
            if v is not None and not isinstance(v, str):
                r[ts_col] = v.isoformat() if hasattr(v, "isoformat") else str(v)
        return r

    def create(
        self, tenant_id: str, values: Dict[str, Any],
        *, _cursor: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Insert one order_case row. ``values`` is a column→value map
        (e.g. ``OrderCase.model_dump()``); only whitelisted columns are
        written. ``tenant_id`` is passed explicitly (tenant-isolation
        guardrail) and overrides any ``tenant_id`` in ``values``."""
        values = {**values, "tenant_id": tenant_id}
        cols = [c for c in self._COLUMNS if c in values]
        params = [
            self._encode_override(values[c]) if c == "pending_override"
            else values[c]
            for c in cols
        ]
        placeholders = ", ".join("?" for _ in cols)
        sql = (
            f"INSERT INTO order_case ({', '.join(cols)}) "
            f"VALUES ({placeholders})"
        )
        if _cursor is not None:
            _cursor.execute(sql, tuple(params))
        else:
            with self._adapter.cursor(tenant_id) as cur:
                cur.execute(sql, tuple(params))
        return self.get(values["case_id"], tenant_id, _cursor=_cursor) or dict(values)

    def get(
        self, case_id: str, tenant_id: Optional[str] = None,
        *, _cursor: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        """Fetch one case by id. ``tenant_id`` optional: the in-memory
        ``CaseStore.get`` takes only ``case_id`` and route call-sites
        check tenant after fetch, so an unscoped lookup (case_id is the
        PK) preserves that contract."""
        sql = f"SELECT {', '.join(self._COLUMNS)} FROM order_case WHERE case_id = ?"
        params: list = [case_id]
        if tenant_id is not None:
            sql += " AND tenant_id = ?"
            params.append(tenant_id)

        if _cursor is not None:
            _cursor.execute(sql, tuple(params))
            row = _cursor.fetchone()
        else:
            with self._adapter.cursor(tenant_id) as cur:
                cur.execute(sql, tuple(params))
                row = cur.fetchone()
        return self._to_dict(row) if row else None

    def list_by_tenant(
        self, tenant_id: str, *, _cursor: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        sql = (
            f"SELECT {', '.join(self._COLUMNS)} FROM order_case "
            f"WHERE tenant_id = ? ORDER BY opened_at"
        )
        if _cursor is not None:
            _cursor.execute(sql, (tenant_id,))
            rows = _cursor.fetchall()
        else:
            with self._adapter.cursor(tenant_id) as cur:
                cur.execute(sql, (tenant_id,))
                rows = cur.fetchall()
        return [self._to_dict(r) for r in rows]

    def update(
        self, case_id: str, tenant_id: str, fields: Dict[str, Any],
        *, _cursor: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        """Apply a partial column update. Only whitelisted columns are
        written; ``case_id`` / ``tenant_id`` are never updated here."""
        cols = [
            c for c in fields
            if c in self._COLUMNS and c not in ("case_id", "tenant_id")
        ]
        if not cols:
            return self.get(case_id, tenant_id, _cursor=_cursor)
        set_clause = ", ".join(f"{c} = ?" for c in cols)
        params = [
            self._encode_override(fields[c]) if c == "pending_override"
            else fields[c]
            for c in cols
        ]
        params.extend([case_id, tenant_id])
        sql = (
            f"UPDATE order_case SET {set_clause} "
            f"WHERE case_id = ? AND tenant_id = ?"
        )
        if _cursor is not None:
            _cursor.execute(sql, tuple(params))
        else:
            with self._adapter.cursor(tenant_id) as cur:
                cur.execute(sql, tuple(params))
        return self.get(case_id, tenant_id, _cursor=_cursor)

    def delete(
        self, case_id: str, tenant_id: str, *, _cursor: Optional[Any] = None,
    ) -> None:
        """Remove a case and its correlation keys. Used by the Phase H.7
        Pass-2 merge to drop a duplicate orphan case after its children
        have been re-pointed. A freshly-backfilled case has no
        classification-history rows, so the V020 FK is not violated;
        children must be re-pointed first or the parent_case_id FK blocks
        the delete (explicit failure — CLAUDE.md §5)."""
        def _run(cur):
            cur.execute(
                "DELETE FROM case_correlation_keys "
                "WHERE case_id = ? AND tenant_id = ?",
                (case_id, tenant_id),
            )
            cur.execute(
                "DELETE FROM order_case WHERE case_id = ? AND tenant_id = ?",
                (case_id, tenant_id),
            )

        if _cursor is not None:
            _run(_cursor)
        else:
            with self._adapter.cursor(tenant_id) as cur:
                _run(cur)

    def set_pending_override_cas(
        self, case_id: str, tenant_id: str, override_json: Optional[str],
        updated_at: str, *, _cursor: Any,
    ) -> int:
        """Forward-only compare-and-set: attach the override only when
        none is in flight. Single conditional UPDATE — atomic on both
        backends (the ``pending_override IS NULL`` guard serialises a
        second initiator without ``SELECT ... FOR UPDATE``, which SQLite
        lacks). Returns the affected row count (0 = case missing or an
        override already exists)."""
        _cursor.execute(
            "UPDATE order_case SET pending_override = ?, "
            "status = 'OPEN_AWAITING_HUMAN', updated_at = ? "
            "WHERE case_id = ? AND tenant_id = ? AND pending_override IS NULL",
            (override_json, updated_at, case_id, tenant_id),
        )
        return _cursor.rowcount


# ---------------------------------------------------------------------------
# Case Correlation Repository (V010) — event→case dedup keys
# ---------------------------------------------------------------------------

class CaseCorrelationRepository:
    """CRUD for ``case_correlation_keys`` (V010).

    Resolves an inbound event to an existing case via the four key types
    in ADR-038 §6.2 priority order. ``register`` upserts (last-writer-wins)
    to mirror the in-memory dict assignment, which silently overwrites a
    duplicate key.
    """

    _PRIORITY = (
        "sales_order_id", "customer_po_number",
        "edi_transaction_id", "source_email_id",
    )

    def __init__(self, adapter=None):
        self._adapter = adapter or create_adapter()

    def register(
        self, tenant_id: str, case_id: str, key_type: str, key_value: str,
        *, _cursor: Optional[Any] = None,
    ) -> None:
        # Upsert: a re-seen key re-points at the (same) case, matching the
        # in-memory ``self._correlation[(...)] = case_id`` overwrite.
        sql = (
            "INSERT INTO case_correlation_keys "
            "(tenant_id, key_type, key_value, case_id, registered_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (tenant_id, key_type, key_value) "
            "DO UPDATE SET case_id = excluded.case_id"
        )
        params = (tenant_id, key_type, key_value, case_id, _now())
        if _cursor is not None:
            _cursor.execute(sql, params)
        else:
            with self._adapter.cursor(tenant_id) as cur:
                cur.execute(sql, params)

    def find_case_id(
        self, tenant_id: str, key_type: str, key_value: str,
        *, _cursor: Optional[Any] = None,
    ) -> Optional[str]:
        sql = (
            "SELECT case_id FROM case_correlation_keys "
            "WHERE tenant_id = ? AND key_type = ? AND key_value = ?"
        )
        params = (tenant_id, key_type, key_value)
        if _cursor is not None:
            _cursor.execute(sql, params)
            row = _cursor.fetchone()
        else:
            with self._adapter.cursor(tenant_id) as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
        if not row:
            return None
        return row["case_id"] if hasattr(row, "keys") else row[0]

    def find_by_priority(
        self, tenant_id: str, *,
        sales_order_id: Optional[str] = None,
        customer_po_number: Optional[str] = None,
        edi_transaction_id: Optional[str] = None,
        source_email_id: Optional[str] = None,
        _cursor: Optional[Any] = None,
    ) -> Optional[str]:
        """First non-null key in SO→PO→EDI→email order that resolves to a
        case wins (ADR-038 §6.2). Returns ``case_id`` or None."""
        candidates = {
            "sales_order_id": sales_order_id,
            "customer_po_number": customer_po_number,
            "edi_transaction_id": edi_transaction_id,
            "source_email_id": source_email_id,
        }
        for key_type in self._PRIORITY:
            key_value = candidates[key_type]
            if not key_value:
                continue
            cid = self.find_case_id(tenant_id, key_type, key_value, _cursor=_cursor)
            if cid is not None:
                return cid
        return None


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
        cur.execute(
            "SELECT event_hash FROM policy_audit_log "
            "WHERE tenant_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (tenant_id,),
        )
        row = cur.fetchone()
        return row[0] if row else "GENESIS"

    def _insert_audit_event(
        self, cur, *, tenant_id: str, policy_key: str,
        previous_value: Optional[str], new_value: str,
        changed_by: str, change_reason: Optional[str],
    ) -> str:
        audit_id = _uuid()
        now = _now()
        prev_hash = self._last_event_hash(cur, tenant_id)
        event_hash = _audit_event_hash(
            prev_hash,
            {
                "id": audit_id, "tenant_id": tenant_id,
                "policy_key": policy_key,
                "previous_value": previous_value, "new_value": new_value,
                "changed_by": changed_by, "change_reason": change_reason,
                "created_at": now, "prev_hash": prev_hash,
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
        self, tenant_id: str, policy_key: str, value: Any,
        created_by: str, change_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        record_id = _uuid()
        now = _now()
        value_json = _json_dumps(value)
        previous = self.get_override(tenant_id, policy_key)
        previous_value = _json_dumps(previous["value"]) if previous else None
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """INSERT INTO policy_overrides
                   (id, tenant_id, policy_key, value, effective_from,
                    created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (record_id, tenant_id, policy_key, value_json, now,
                 created_by, now),
            )
            self._insert_audit_event(
                cur, tenant_id=tenant_id, policy_key=policy_key,
                previous_value=previous_value, new_value=value_json,
                changed_by=created_by, change_reason=change_reason,
            )
        return {
            "id": record_id, "tenant_id": tenant_id,
            "policy_key": policy_key, "value": value,
            "effective_from": now, "created_by": created_by,
        }

    def create_audit_event(
        self, tenant_id: str, policy_key: str,
        previous_value: Any, new_value: Any,
        changed_by: str, change_reason: Optional[str] = None,
    ) -> str:
        with self._adapter.cursor(tenant_id) as cur:
            return self._insert_audit_event(
                cur, tenant_id=tenant_id, policy_key=policy_key,
                previous_value=_json_dumps(previous_value)
                    if previous_value is not None else None,
                new_value=_json_dumps(new_value),
                changed_by=changed_by, change_reason=change_reason,
            )

    def verify_audit_chain(self, tenant_id: str) -> tuple[bool, Optional[int]]:
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
                    "id": audit_id, "tenant_id": t_id,
                    "policy_key": policy_key,
                    "previous_value": previous_value, "new_value": new_value,
                    "changed_by": changed_by, "change_reason": change_reason,
                    "created_at": created_at, "prev_hash": prev_hash,
                },
            )
            if recomputed != event_hash:
                return False, i
            expected_prev = event_hash
        return True, None

    def get_override(self, tenant_id: str, policy_key: str) -> Optional[Dict[str, Any]]:
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
        self,
        tenant_id: str,
        limit: int = 50,
        policy_key_prefix: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List audit-log rows, newest first.

        ``policy_key_prefix`` (PR-C.2): when set, restricts the result
        to rows whose ``policy_key`` LIKE ``{prefix}%``. Used by the
        config-audit endpoint to scope to ``duplicate_po.weights.*``
        without a Python-side filter pass.
        """
        conditions = ["tenant_id = ?"]
        params: list = [tenant_id]
        if policy_key_prefix:
            conditions.append("policy_key LIKE ?")
            params.append(f"{policy_key_prefix}%")
        where = " AND ".join(conditions)
        params.append(limit)

        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                f"""SELECT id, tenant_id, policy_key, previous_value,
                          new_value, changed_by, change_reason, created_at,
                          prev_hash, event_hash
                   FROM policy_audit_log
                   WHERE {where}
                   ORDER BY created_at DESC LIMIT ?""",
                tuple(params),
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
                    pass
            if isinstance(r.get("new_value"), str):
                try:
                    r["new_value"] = _json_loads(r["new_value"])
                except Exception:
                    pass
            results.append(r)
        return results


# ---------------------------------------------------------------------------
# Tenant Config Repository (ADR-030 — 5-level hierarchy, layers 2-5)
# ---------------------------------------------------------------------------
#
# Layer 1 (platform) lives on disk in gateways/configs/duplicate_po/defaults.json
# and is read by gateways/tenant_config.py. Layers 2-5 (tenant / tier /
# customer / channel) live in the tenant_config table and are managed
# through this repository.
#
# Each row carries a partial weight map for its scope; resolution is
# performed by gateways/tenant_config.py::resolve_weights(). The audit
# history of every edit flows through PolicyRepository.create_audit_event
# so the existing hash-chained policy_audit_log is the single SOX surface
# for both policy threshold tunings and config-weight edits.

_TENANT_CONFIG_VALID_LAYERS = ("tenant", "tier", "customer", "channel")


def _canonical_scope(scope: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Drop None values so the hash is stable across callers that
    pass {"customer_id": None} vs {} for the tenant layer."""
    return {k: v for k, v in (scope or {}).items() if v is not None}


def _scope_hash(scope: Optional[Dict[str, Any]]) -> str:
    """SHA-256 of canonical-JSON scope. Stable across Python invocations."""
    canonical = _canonical_scope(scope)
    payload = json.dumps(canonical, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TenantConfigRepository:
    """CRUD for the ``tenant_config`` table (ADR-030 layers 2-5).

    The four supported layers and their scope shapes:
      * ``tenant``   — scope = {} (one row per tenant)
      * ``tier``     — scope = {"customer_tier": "strategic"|"standard"|"smb"}
      * ``customer`` — scope = {"customer_id": "..."}
      * ``channel``  — scope = {"customer_id": "...", "channel": "..."}

    Methods are intentionally narrow: upsert / get / list_by_layer /
    delete cover the V1 admin-tooling surface; resolve_layered_overrides
    fans the four reads needed by the gateway resolver in a single
    round-trip.

    Audit-chain coupling is the route handler's responsibility (see
    api/routes/config.py): on every upsert/delete it calls
    PolicyRepository.create_audit_event with a ConfigChangeEvent
    payload and the canonical policy_key produced by
    contracts.config_events.policy_key_for_event.
    """

    def __init__(self, adapter=None):
        self._adapter = adapter or create_adapter()

    @staticmethod
    def _validate_layer(layer: str) -> None:
        if layer not in _TENANT_CONFIG_VALID_LAYERS:
            raise ValueError(
                f"invalid layer {layer!r}; expected one of "
                f"{_TENANT_CONFIG_VALID_LAYERS}"
            )

    def upsert(
        self, tenant_id: str, layer: str,
        scope: Optional[Dict[str, Any]],
        weights: Dict[str, float], created_by: str,
    ) -> Dict[str, Any]:
        self._validate_layer(layer)
        canonical_scope = _canonical_scope(scope)
        sh = _scope_hash(canonical_scope)
        now = _now()
        scope_json = _json_dumps(canonical_scope)
        weights_json = _json_dumps(weights)

        existing = self.get(tenant_id, layer, canonical_scope)
        with self._adapter.cursor(tenant_id) as cur:
            if existing:
                cur.execute(
                    """UPDATE tenant_config
                          SET weights = ?, created_by = ?, updated_at = ?
                        WHERE tenant_id = ? AND layer = ? AND scope_hash = ?""",
                    (weights_json, created_by, now, tenant_id, layer, sh),
                )
                record_id = existing["id"]
                created_at = existing["created_at"]
            else:
                record_id = _uuid()
                created_at = now
                cur.execute(
                    """INSERT INTO tenant_config
                       (id, tenant_id, layer, scope_hash, scope, weights,
                        created_by, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (record_id, tenant_id, layer, sh, scope_json,
                     weights_json, created_by, created_at, now),
                )
        return {
            "id": record_id, "tenant_id": tenant_id, "layer": layer,
            "scope_hash": sh, "scope": canonical_scope,
            "weights": weights, "created_by": created_by,
            "created_at": created_at, "updated_at": now,
        }

    def get(self, tenant_id: str, layer: str,
            scope: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        self._validate_layer(layer)
        sh = _scope_hash(scope)
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """SELECT id, tenant_id, layer, scope_hash, scope, weights,
                          created_by, created_at, updated_at
                     FROM tenant_config
                    WHERE tenant_id = ? AND layer = ? AND scope_hash = ?""",
                (tenant_id, layer, sh),
            )
            row = cur.fetchone()
        if not row:
            return None
        return self._to_dict(row)

    def list_by_layer(self, tenant_id: str, layer: str) -> List[Dict[str, Any]]:
        self._validate_layer(layer)
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """SELECT id, tenant_id, layer, scope_hash, scope, weights,
                          created_by, created_at, updated_at
                     FROM tenant_config
                    WHERE tenant_id = ? AND layer = ?
                    ORDER BY created_at, id""",
                (tenant_id, layer),
            )
            rows = cur.fetchall()
        return [self._to_dict(r) for r in rows]

    def delete(self, tenant_id: str, layer: str,
               scope: Optional[Dict[str, Any]]) -> bool:
        self._validate_layer(layer)
        existing = self.get(tenant_id, layer, scope)
        if not existing:
            return False
        sh = existing["scope_hash"]
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """DELETE FROM tenant_config
                    WHERE tenant_id = ? AND layer = ? AND scope_hash = ?""",
                (tenant_id, layer, sh),
            )
        return True

    def resolve_layered_overrides(
        self, tenant_id: str,
        customer_tier: Optional[str] = None,
        customer_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> Dict[str, Dict[str, float]]:
        result: Dict[str, Dict[str, float]] = {
            "tenant": {}, "tier": {}, "customer": {}, "channel": {},
        }
        tenant_row = self.get(tenant_id, "tenant", {})
        if tenant_row:
            result["tenant"] = tenant_row["weights"]
        if customer_tier:
            tier_row = self.get(tenant_id, "tier",
                                {"customer_tier": customer_tier})
            if tier_row:
                result["tier"] = tier_row["weights"]
        if customer_id:
            cust_row = self.get(tenant_id, "customer",
                                {"customer_id": customer_id})
            if cust_row:
                result["customer"] = cust_row["weights"]
        if customer_id and channel:
            chan_row = self.get(
                tenant_id, "channel",
                {"customer_id": customer_id, "channel": channel},
            )
            if chan_row:
                result["channel"] = chan_row["weights"]
        return result

    _COLUMNS = (
        "id", "tenant_id", "layer", "scope_hash", "scope", "weights",
        "created_by", "created_at", "updated_at",
    )

    def _to_dict(self, row) -> Dict[str, Any]:
        r = _row_to_dict(row, self._COLUMNS)
        for json_col in ("scope", "weights"):
            if isinstance(r.get(json_col), str):
                r[json_col] = _json_loads(r[json_col]) or {}
        if r.get("id") is not None and not isinstance(r["id"], str):
            r["id"] = str(r["id"])
        for ts_col in ("created_at", "updated_at"):
            v = r.get(ts_col)
            if v is not None and not isinstance(v, str):
                r[ts_col] = v.isoformat() if hasattr(v, "isoformat") else str(v)
        return r


# ---------------------------------------------------------------------------
# Case Event Repository — ADR-038 Phase H.5 replay log (V012)
# ---------------------------------------------------------------------------


class CaseEventRepository:
    """Append-only persistence for `case_events`.

    The agent's `(tool_call, tool_result)` pairs land here so audit
    replay reads from a single canonical source. Schema mirrors
    `agents.harness.ToolCallReplayEntry` field-for-field.
    """

    def __init__(self, adapter=None):
        self._adapter = adapter or create_adapter()

    def record(
        self,
        *,
        event_id: str,
        case_id: str,
        tenant_id: str,
        occurred_at: str,
        tool_name: str,
        tool_call: Dict[str, Any],
        tool_result: Dict[str, Any],
        outcome: str,
    ) -> None:
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """INSERT INTO case_events
                   (event_id, case_id, tenant_id, occurred_at,
                    tool_name, tool_call, tool_result, outcome)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, case_id, tenant_id, occurred_at,
                 tool_name, _json_dumps(tool_call),
                 _json_dumps(tool_result), outcome),
            )

    def list_for_case(
        self, case_id: str, tenant_id: str,
    ) -> List[Dict[str, Any]]:
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """SELECT event_id, case_id, tenant_id, occurred_at,
                          tool_name, tool_call, tool_result, outcome
                   FROM case_events
                   WHERE case_id = ? AND tenant_id = ?
                   ORDER BY occurred_at ASC""",
                (case_id, tenant_id),
            )
            rows = cur.fetchall()
        return [self._to_event_dict(r) for r in rows]

    _EVENT_COLUMNS = (
        "event_id", "case_id", "tenant_id", "occurred_at",
        "tool_name", "tool_call", "tool_result", "outcome",
    )

    def _to_event_dict(self, row) -> Dict[str, Any]:
        r = _row_to_dict(row, self._EVENT_COLUMNS)
        for json_col in ("tool_call", "tool_result"):
            if isinstance(r.get(json_col), str):
                r[json_col] = _json_loads(r[json_col]) or {}
        return r


# ---------------------------------------------------------------------------
# Case Lock Repository — ADR-038 Phase H.5 cross-pod mutex (V013)
# ---------------------------------------------------------------------------


class CaseLockRepository:
    """Cross-pod mutex via INSERT-with-UNIQUE-conflict on `case_locks`.

    The acquire path INSERTs a row keyed on `case_id`; on a UNIQUE
    constraint violation the lock is held. Release deletes the row.
    Stale rows are cleaned by a separate janitor query
    (`sweep_expired`) that ops can run on a schedule.
    """

    DEFAULT_TTL_SECONDS = 60  # bound by max harness step latency

    def __init__(self, adapter=None):
        self._adapter = adapter or create_adapter()

    def try_acquire(
        self,
        case_id: str,
        tenant_id: str,
        *,
        acquired_by: Optional[str] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> bool:
        """Atomically try to claim the lock. Returns True on
        success; False when another holder has it (UNIQUE
        violation on the PK)."""
        from datetime import timedelta
        now_dt = datetime.now(timezone.utc)
        expires_at = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
        # Sweep stale entries for THIS case first — avoids an
        # ops-needed window when a process exited mid-step. The
        # tenant_id predicate is mandatory (PR-tenant-isolation
        # repo invariant) and a `case_locks.case_id` is globally
        # unique (UUID) so the narrowing is also semantically correct.
        try:
            with self._adapter.cursor(tenant_id) as cur:
                cur.execute(
                    """DELETE FROM case_locks
                       WHERE case_id = ? AND tenant_id = ?
                         AND expires_at < ?""",
                    (case_id, tenant_id, now_dt.isoformat()),
                )
                cur.execute(
                    """INSERT INTO case_locks
                       (case_id, tenant_id, acquired_at, acquired_by, expires_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (case_id, tenant_id, now_dt.isoformat(),
                     acquired_by, expires_at),
                )
            return True
        except Exception as exc:  # noqa: BLE001 — UNIQUE conflict
            # Both psycopg2 IntegrityError and sqlite3.IntegrityError
            # land here; we avoid driver-specific exception types
            # at this layer.
            msg = str(exc).lower()
            if "unique" in msg or "duplicate" in msg or "integrity" in msg:
                return False
            raise

    def release(self, case_id: str, tenant_id: str) -> None:
        """Release the lock unconditionally. Idempotent — releasing
        a non-existent lock is a no-op."""
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                "DELETE FROM case_locks WHERE case_id = ? AND tenant_id = ?",
                (case_id, tenant_id),
            )

    def sweep_expired(self, tenant_id: str) -> int:
        """Janitor: drop every lock past its TTL for the tenant.
        Returns the number of rows deleted (0 on backends that
        don't expose `rowcount` reliably)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """DELETE FROM case_locks
                   WHERE tenant_id = ? AND expires_at < ?""",
                (tenant_id, now_iso),
            )
            count = cur.rowcount if hasattr(cur, "rowcount") else 0
        return int(count or 0)


# ---------------------------------------------------------------------------
# Effect Outbox Repository (DoR #6 — durable compensation ledger)
# ---------------------------------------------------------------------------

class OutboxRepository:
    """CRUD for the ``effect_outbox`` table.

    Dict-based by design — the DB layer stays independent of
    ``orchestration.outbox`` (which adapts these dicts to/from its
    ``OutboxEntry`` DTO). Booleans are stored as native bool (Postgres
    ``BOOLEAN``; SQLite coerces to 0/1) and coerced back on read.
    """

    _COLUMNS = (
        "id", "tenant_id", "trace_id", "recipe", "gateway", "operation",
        "status", "recipe_status", "committed", "needs_compensation", "error",
        "params", "attempts", "escalated", "compensated", "compensated_at",
        "created_at",
    )

    def __init__(self, adapter=None):
        self._adapter = adapter or create_adapter()

    def insert(self, tenant_id: str, entry: Dict[str, Any]) -> None:
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """INSERT INTO effect_outbox
                   (id, tenant_id, trace_id, recipe, gateway, operation,
                    status, recipe_status, committed, needs_compensation,
                    error, params, attempts, escalated, compensated,
                    compensated_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry["id"], entry["tenant_id"], entry.get("trace_id"),
                    entry.get("recipe"), entry["gateway"], entry["operation"],
                    entry["status"], entry.get("recipe_status"),
                    bool(entry.get("committed")), bool(entry.get("needs_compensation")),
                    entry.get("error"), _json_dumps(entry.get("params") or {}),
                    int(entry.get("attempts", 0)), bool(entry.get("escalated")),
                    bool(entry.get("compensated")), entry.get("compensated_at"),
                    entry["created_at"],
                ),
            )

    def list_pending(self, tenant_id: str) -> List[Dict[str, Any]]:
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """SELECT * FROM effect_outbox
                   WHERE tenant_id = ? AND needs_compensation = ?
                     AND compensated = ? AND escalated = ?
                   ORDER BY created_at""",
                (tenant_id, True, False, False),
            )
            return [self._to_dict(r) for r in cur.fetchall()]

    def list_all(self, tenant_id: str) -> List[Dict[str, Any]]:
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                "SELECT * FROM effect_outbox WHERE tenant_id = ? ORDER BY created_at",
                (tenant_id,),
            )
            return [self._to_dict(r) for r in cur.fetchall()]

    def increment_attempts(self, tenant_id: str, entry_id: str) -> int:
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                "UPDATE effect_outbox SET attempts = attempts + 1 WHERE id = ? AND tenant_id = ?",
                (entry_id, tenant_id),
            )
            cur.execute(
                "SELECT attempts FROM effect_outbox WHERE id = ? AND tenant_id = ?",
                (entry_id, tenant_id),
            )
            row = cur.fetchone()
        if row is None:
            return 0
        return int(row[0] if not hasattr(row, "keys") else row["attempts"])

    def mark_compensated(self, tenant_id: str, entry_id: str, compensated_at: str) -> bool:
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                "UPDATE effect_outbox SET compensated = ?, compensated_at = ? WHERE id = ? AND tenant_id = ?",
                (True, compensated_at, entry_id, tenant_id),
            )
            return bool(getattr(cur, "rowcount", 0))

    def mark_escalated(self, tenant_id: str, entry_id: str) -> bool:
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                "UPDATE effect_outbox SET escalated = ? WHERE id = ? AND tenant_id = ?",
                (True, entry_id, tenant_id),
            )
            return bool(getattr(cur, "rowcount", 0))

    def _to_dict(self, row) -> Dict[str, Any]:
        r = _row_to_dict(row, self._COLUMNS)
        if isinstance(r.get("params"), str):
            r["params"] = _json_loads(r["params"]) or {}
        elif r.get("params") is None:
            r["params"] = {}
        for bool_col in ("committed", "needs_compensation", "escalated", "compensated"):
            r[bool_col] = bool(r.get(bool_col))
        if r.get("attempts") is None:
            r["attempts"] = 0
        return r


class AttachmentRepository:
    """CRUD for the ``email_attachment`` table (V016).

    Dict-based by design — the DB layer stays independent of
    ``gateways.attachment_store`` (which adapts these dicts to/from its
    ``AttachmentRecord`` DTO). ``content`` is the raw attachment bytes (BYTEA on
    Postgres, BLOB on SQLite; integrity is the stored ``sha256``). The blob is
    SELECTed only by ``get`` (single fetch); ``list_for_case`` returns
    metadata only so listing never drags blobs. All queries are tenant-scoped.
    """

    _COLUMNS = (
        "id", "tenant_id", "case_id", "name", "mime_type", "size_bytes",
        "sha256", "content", "created_at",
    )
    # Metadata projection — everything except the blob, for list reads.
    _META_COLUMNS = (
        "id", "tenant_id", "case_id", "name", "mime_type", "size_bytes",
        "sha256", "created_at",
    )

    def __init__(self, adapter=None):
        self._adapter = adapter or create_adapter()

    def insert(self, tenant_id: str, rec: Dict[str, Any]) -> None:
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """INSERT INTO email_attachment
                   (id, tenant_id, case_id, name, mime_type, size_bytes,
                    sha256, content, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rec["id"], rec["tenant_id"], rec.get("case_id"),
                    rec["name"], rec["mime_type"], int(rec["size_bytes"]),
                    rec["sha256"], rec["content"], rec["created_at"],
                ),
            )

    def get(self, tenant_id: str, attachment_id: str) -> Optional[Dict[str, Any]]:
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """SELECT id, tenant_id, case_id, name, mime_type, size_bytes,
                          sha256, content, created_at
                   FROM email_attachment WHERE id = ? AND tenant_id = ?""",
                (attachment_id, tenant_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        rec = _row_to_dict(row, self._COLUMNS)
        if rec.get("content") is not None:
            rec["content"] = bytes(rec["content"])  # memoryview (psycopg2) → bytes
        return rec

    def list_for_case(self, tenant_id: str, case_id: str) -> List[Dict[str, Any]]:
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                """SELECT id, tenant_id, case_id, name, mime_type, size_bytes,
                          sha256, created_at
                   FROM email_attachment
                   WHERE tenant_id = ? AND case_id = ? ORDER BY created_at""",
                (tenant_id, case_id),
            )
            return [_row_to_dict(r, self._META_COLUMNS) for r in cur.fetchall()]

    def delete(self, tenant_id: str, attachment_id: str) -> None:
        """Hard-delete the row for right-to-erasure (ADR-044 §2.4). Tenant-scoped
        so an id alone can't erase another tenant's attachment. The PII-free
        tombstone is written by ``attachment_store.erase_attachment`` against the
        record read before this delete — the immutable audit chain is untouched."""
        with self._adapter.cursor(tenant_id) as cur:
            cur.execute(
                "DELETE FROM email_attachment WHERE id = ? AND tenant_id = ?",
                (attachment_id, tenant_id),
            )
