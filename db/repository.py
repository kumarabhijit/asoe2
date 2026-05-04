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
            "id": record_id, "tenant_id": tenant_id, "order_id": order_id,
            "event_type": event_type, "intent": intent,
            "lifecycle_state": lifecycle_state, "shadow_verdict": shadow_verdict,
            "selected_recipe": selected_recipe, "final_status": final_status,
            "trace_id": trace_id, "resolution_data": resolution_data or {},
            "original_event": original_event,
            "reanalysis_history": reanalysis_history or [],
            "enrichment_context": enrichment_context or {},
            "resolved_by": None, "resolved_action": None, "resolution_notes": None,
            "created_at": now, "updated_at": now,
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

    def update(self, exception_id: str, tenant_id: str, **fields) -> Optional[Dict[str, Any]]:
        if not fields:
            return self.get(exception_id, tenant_id)
        for json_col in ("resolution_data", "original_event",
                         "reanalysis_history", "enrichment_context"):
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
        "created_at", "updated_at",
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
