"""Database migration runner.

Applies SQL migrations to PostgreSQL or SQLite databases.
PostgreSQL uses the full migration SQL (V001__initial_schema.sql).
SQLite uses a compatible subset (no extensions, RLS, triggers, or VECTOR).

Usage:
    # Apply migrations to PostgreSQL
    DATABASE_URL=postgresql://... python -m db.migrations.runner

    # Apply migrations to SQLite (testing)
    DATABASE_URL=sqlite:///path/to/test.db python -m db.migrations.runner
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger("asoe.db.migrations")

_MIGRATIONS_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# SQLite-compatible schema (subset of V001)
# ---------------------------------------------------------------------------

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS exceptions (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    order_id          TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    intent            TEXT,
    lifecycle_state   TEXT NOT NULL DEFAULT 'INGESTED',
    shadow_verdict    TEXT,
    selected_recipe   TEXT,
    final_status      TEXT,
    trace_id          TEXT NOT NULL,
    resolution_data   TEXT DEFAULT '{}',
    resolved_by       TEXT,
    resolved_action   TEXT,
    resolution_notes  TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exceptions_tenant_state
    ON exceptions (tenant_id, lifecycle_state, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_exceptions_trace
    ON exceptions (trace_id);
CREATE INDEX IF NOT EXISTS idx_exceptions_order
    ON exceptions (tenant_id, order_id);

CREATE TABLE IF NOT EXISTS traces (
    id              TEXT PRIMARY KEY,
    exception_id    TEXT NOT NULL REFERENCES exceptions(id),
    trace_id        TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,
    trace_record    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_traces_trace_id
    ON traces (trace_id);
CREATE INDEX IF NOT EXISTS idx_traces_tenant
    ON traces (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS policy_overrides (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    policy_key      TEXT NOT NULL,
    value           TEXT NOT NULL,
    effective_from  TEXT NOT NULL,
    effective_until TEXT,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(tenant_id, policy_key, effective_from)
);

CREATE TABLE IF NOT EXISTS policy_audit_log (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    policy_key      TEXT NOT NULL,
    previous_value  TEXT,
    new_value       TEXT NOT NULL,
    changed_by      TEXT NOT NULL,
    change_reason   TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_policy_audit_tenant
    ON policy_audit_log (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS checkpoints (
    trace_id        TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    graph_state     TEXT NOT NULL,
    interrupted_at  TEXT NOT NULL,
    resumed_at      TEXT,
    resumed_by      TEXT,
    status          TEXT NOT NULL DEFAULT 'PENDING'
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_pending
    ON checkpoints (status, interrupted_at);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TEXT NOT NULL
);
"""


def _sqlite_column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def _sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def _apply_sqlite_v002(conn: sqlite3.Connection) -> None:
    if not _sqlite_column_exists(conn, "exceptions", "original_event"):
        conn.execute("ALTER TABLE exceptions ADD COLUMN original_event TEXT")
    if not _sqlite_column_exists(conn, "exceptions", "reanalysis_history"):
        conn.execute(
            "ALTER TABLE exceptions ADD COLUMN reanalysis_history TEXT "
            "NOT NULL DEFAULT '[]'"
        )
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V002", now),
    )
    conn.commit()
    logger.info("SQLite schema V002 applied")


def _audit_event_hash(prev_hash: str, row: dict) -> str:
    import hashlib
    import json as _json
    fields = {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "policy_key": row["policy_key"],
        "previous_value": row.get("previous_value"),
        "new_value": row["new_value"],
        "changed_by": row["changed_by"],
        "change_reason": row.get("change_reason"),
        "created_at": row["created_at"],
        "prev_hash": prev_hash,
    }
    payload = _json.dumps(
        {k: fields[k] for k in sorted(fields)},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256((prev_hash + "|" + payload).encode("utf-8")).hexdigest()


def _apply_sqlite_v003(conn: sqlite3.Connection) -> None:
    if not _sqlite_column_exists(conn, "policy_audit_log", "prev_hash"):
        conn.execute(
            "ALTER TABLE policy_audit_log ADD COLUMN prev_hash TEXT "
            "NOT NULL DEFAULT 'GENESIS'"
        )
    if not _sqlite_column_exists(conn, "policy_audit_log", "event_hash"):
        conn.execute(
            "ALTER TABLE policy_audit_log ADD COLUMN event_hash TEXT "
            "NOT NULL DEFAULT ''"
        )
    cur = conn.execute(
        "SELECT id, tenant_id, policy_key, previous_value, new_value, "
        "       changed_by, change_reason, created_at "
        "FROM policy_audit_log "
        "WHERE event_hash = '' "
        "ORDER BY tenant_id, created_at, id"
    )
    rows = [
        dict(
            id=r[0], tenant_id=r[1], policy_key=r[2],
            previous_value=r[3], new_value=r[4],
            changed_by=r[5], change_reason=r[6], created_at=r[7],
        )
        for r in cur.fetchall()
    ]
    last_hash_by_tenant: dict[str, str] = {}
    for row in rows:
        prev = last_hash_by_tenant.get(row["tenant_id"], "GENESIS")
        h = _audit_event_hash(prev, row)
        conn.execute(
            "UPDATE policy_audit_log SET prev_hash = ?, event_hash = ? "
            "WHERE id = ?",
            (prev, h, row["id"]),
        )
        last_hash_by_tenant[row["tenant_id"]] = h

    conn.executescript(
        """
        DROP TRIGGER IF EXISTS policy_audit_log_no_update;
        CREATE TRIGGER policy_audit_log_no_update
        BEFORE UPDATE ON policy_audit_log
        BEGIN
            SELECT RAISE(ABORT, 'policy_audit_log is append-only; UPDATE rejected');
        END;

        DROP TRIGGER IF EXISTS policy_audit_log_no_delete;
        CREATE TRIGGER policy_audit_log_no_delete
        BEFORE DELETE ON policy_audit_log
        BEGIN
            SELECT RAISE(ABORT, 'policy_audit_log is append-only; DELETE rejected');
        END;

        CREATE INDEX IF NOT EXISTS idx_policy_audit_chain
            ON policy_audit_log (tenant_id, created_at, id);
        """
    )
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V003", now),
    )
    conn.commit()
    logger.info("SQLite schema V003 applied (hash-chained policy_audit_log)")


def _apply_sqlite_v004(conn: sqlite3.Connection) -> None:
    if not _sqlite_column_exists(conn, "exceptions", "enrichment_context"):
        conn.execute(
            "ALTER TABLE exceptions ADD COLUMN enrichment_context TEXT "
            "NOT NULL DEFAULT '{}'"
        )
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V004", now),
    )
    conn.commit()
    logger.info("SQLite schema V004 applied (enrichment_context column)")


def _apply_sqlite_v006(conn: sqlite3.Connection) -> None:
    """V006 — tenant_config table for ADR-030 5-level hierarchy storage."""
    if not _sqlite_table_exists(conn, "tenant_config"):
        conn.executescript(
            """
            CREATE TABLE tenant_config (
                id              TEXT PRIMARY KEY,
                tenant_id       TEXT NOT NULL,
                layer           TEXT NOT NULL,
                scope_hash      TEXT NOT NULL,
                scope           TEXT NOT NULL DEFAULT '{}',
                weights         TEXT NOT NULL,
                created_by      TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                UNIQUE (tenant_id, layer, scope_hash),
                CHECK (layer IN ('tenant','tier','customer','channel'))
            );

            CREATE INDEX IF NOT EXISTS idx_tenant_config_lookup
                ON tenant_config (tenant_id, layer);
            """
        )
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V006", now),
    )
    conn.commit()
    logger.info("SQLite schema V006 applied (tenant_config table)")


def _apply_sqlite_v007(conn: sqlite3.Connection) -> None:
    """V007 — DB-level metadata-contract enforcement for DUPLICATE_PO rows.

    Mirrors V007__duplicate_po_metadata_contract.sql. SQLite triggers
    use ``json_extract`` instead of the Postgres jsonb ? operator and
    use ``RAISE(ABORT, ...)`` instead of plpgsql ``RAISE EXCEPTION``.

    Rejects DUPLICATE_PO rows that have reached a terminal state
    (final_status IS NOT NULL) but are missing any of the four
    contract-required resolution_data keys: signal_breakdown,
    composite_score, classification, recommended_action.

    The trigger runs on both INSERT and UPDATE; SQLite syntax requires
    a separate CREATE TRIGGER statement per event so we declare two.
    """
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS exceptions_duplicate_po_metadata_contract_insert;
        CREATE TRIGGER exceptions_duplicate_po_metadata_contract_insert
        BEFORE INSERT ON exceptions
        WHEN NEW.intent = 'DUPLICATE_PO' AND NEW.final_status IS NOT NULL
        BEGIN
            SELECT CASE
                WHEN NEW.resolution_data IS NULL
                  OR json_extract(NEW.resolution_data, '$.signal_breakdown') IS NULL
                    THEN RAISE(ABORT, 'metadata-contract violation: DUPLICATE_PO row missing resolution_data.signal_breakdown')
                WHEN json_extract(NEW.resolution_data, '$.composite_score') IS NULL
                    THEN RAISE(ABORT, 'metadata-contract violation: DUPLICATE_PO row missing resolution_data.composite_score')
                WHEN json_extract(NEW.resolution_data, '$.classification') IS NULL
                    THEN RAISE(ABORT, 'metadata-contract violation: DUPLICATE_PO row missing resolution_data.classification')
                WHEN json_extract(NEW.resolution_data, '$.recommended_action') IS NULL
                    THEN RAISE(ABORT, 'metadata-contract violation: DUPLICATE_PO row missing resolution_data.recommended_action')
            END;
        END;

        DROP TRIGGER IF EXISTS exceptions_duplicate_po_metadata_contract_update;
        CREATE TRIGGER exceptions_duplicate_po_metadata_contract_update
        BEFORE UPDATE ON exceptions
        WHEN NEW.intent = 'DUPLICATE_PO' AND NEW.final_status IS NOT NULL
        BEGIN
            SELECT CASE
                WHEN NEW.resolution_data IS NULL
                  OR json_extract(NEW.resolution_data, '$.signal_breakdown') IS NULL
                    THEN RAISE(ABORT, 'metadata-contract violation: DUPLICATE_PO row missing resolution_data.signal_breakdown')
                WHEN json_extract(NEW.resolution_data, '$.composite_score') IS NULL
                    THEN RAISE(ABORT, 'metadata-contract violation: DUPLICATE_PO row missing resolution_data.composite_score')
                WHEN json_extract(NEW.resolution_data, '$.classification') IS NULL
                    THEN RAISE(ABORT, 'metadata-contract violation: DUPLICATE_PO row missing resolution_data.classification')
                WHEN json_extract(NEW.resolution_data, '$.recommended_action') IS NULL
                    THEN RAISE(ABORT, 'metadata-contract violation: DUPLICATE_PO row missing resolution_data.recommended_action')
            END;
        END;
        """
    )
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V007", now),
    )
    conn.commit()
    logger.info(
        "SQLite schema V007 applied (DUPLICATE_PO metadata-contract trigger)",
    )


def _apply_sqlite_v008(conn: sqlite3.Connection) -> None:
    """V008 — Backfill resolution_data.line_items for legacy exception rows.

    Mirrors V008__backfill_line_items.sql. SQLite stores
    resolution_data and original_event as TEXT (JSON-stringified); we
    use json_extract / json_each / json_set / json_object /
    json_group_array to project LineItem rows from each record's
    original_event when line_items is missing.

    Two projection modes (mirroring
    `api/routes/exceptions.py::_project_line_items`):

      1. **Multi-line** — when `original_event.metadata.line_items`
         is a non-empty array, expand each entry into its own
         LineItem with order-level fallbacks for omitted fields.
      2. **Single-line fallback** — emit one LineItem from the
         event's order-level fields.

    Idempotent: re-runs are no-ops thanks to the WHERE-clause check.

    V007 trigger compatibility: pre-V007 DUPLICATE_PO rows that lack
    the four audit-bearing keys would trip the trigger and abort the
    migration. The WHERE clause excludes them.

    Implementation note: SQLite doesn't support UPDATE-FROM-CTE the
    way Postgres does, so this is implemented as a two-pass operation
    inside Python — read candidate rows, project per-row, write back.
    Idempotency is preserved via the same WHERE-clause filter the
    Postgres path uses.
    """
    # ── Pass 1: enumerate candidate rows ──────────────────────────────
    cursor = conn.execute(
        """
        SELECT id, original_event, resolution_data, intent, final_status
        FROM exceptions
        WHERE original_event IS NOT NULL
          AND (
              resolution_data IS NULL
              OR json_extract(resolution_data, '$.line_items') IS NULL
              OR json_type(resolution_data, '$.line_items') != 'array'
              OR json_array_length(resolution_data, '$.line_items') = 0
          )
          AND (
              intent != 'DUPLICATE_PO'
              OR final_status IS NULL
              OR (
                  json_extract(resolution_data, '$.signal_breakdown') IS NOT NULL
                  AND json_extract(resolution_data, '$.composite_score') IS NOT NULL
                  AND json_extract(resolution_data, '$.classification') IS NOT NULL
                  AND json_extract(resolution_data, '$.recommended_action') IS NOT NULL
              )
          )
        """
    )
    candidates = cursor.fetchall()

    # ── Pass 2: project + write back ──────────────────────────────────
    import json as _json

    backfilled = 0
    for row in candidates:
        rec_id, evt_text, rd_text, _intent, _final = row
        try:
            evt = _json.loads(evt_text) if evt_text else {}
        except (TypeError, _json.JSONDecodeError):
            continue
        rd = {}
        if rd_text:
            try:
                rd = _json.loads(rd_text)
            except (TypeError, _json.JSONDecodeError):
                rd = {}

        order_id = evt.get("order_id", "") or ""
        fallback_sku = evt.get("sku") or order_id
        fallback_desc = evt.get("event_type") or "Order line"
        fallback_qty = int(evt.get("line_count") or 1)
        fallback_erp = float(evt.get("sap_base_price") or 0.0)
        fallback_po = float(evt.get("po_price") or 0.0)
        event_line_item = int(evt.get("line_item") or 1)

        meta = evt.get("metadata") or {}
        raw_lines = meta.get("line_items") if isinstance(meta, dict) else None

        items: list[dict] = []
        if isinstance(raw_lines, list) and raw_lines:
            for idx, raw in enumerate(raw_lines, start=1):
                if not isinstance(raw, dict):
                    continue
                line_no = raw.get("line_id") or f"{order_id}-{raw.get('line_item', idx)}"
                items.append({
                    "line_id": str(line_no),
                    "sku": str(raw.get("sku") or fallback_sku),
                    "description": str(raw.get("description") or fallback_desc),
                    "uom": str(raw.get("uom") or "EA"),
                    "quantity": int(raw.get("quantity", fallback_qty) or fallback_qty),
                    "erp_price": float(raw.get("erp_price", fallback_erp) or fallback_erp),
                    "po_price": float(raw.get("po_price", fallback_po) or fallback_po),
                    "root_cause": raw.get("root_cause"),
                })
        if not items:
            items = [{
                "line_id": f"{order_id}-{event_line_item}",
                "sku": fallback_sku,
                "description": fallback_desc,
                "uom": "EA",
                "quantity": fallback_qty,
                "erp_price": fallback_erp,
                "po_price": fallback_po,
            }]

        rd["line_items"] = items
        conn.execute(
            "UPDATE exceptions SET resolution_data = ? WHERE id = ?",
            (_json.dumps(rd), rec_id),
        )
        backfilled += 1

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V008", now),
    )
    conn.commit()
    logger.info(
        "SQLite schema V008 applied (backfill resolution_data.line_items, "
        "%d row%s updated)", backfilled, "" if backfilled == 1 else "s",
    )


def _apply_sqlite_v009(conn: sqlite3.Connection) -> None:
    """V009 — order_case parent entity + parent_case_id on exceptions.

    Folded into the SQLite path so V018 (which alters order_case) has a
    table to alter on both backends. Mirrors V009__order_case.sql with
    the same column shape; CHECK constraints kept as code-level
    invariants to match V005's deliberate avoidance of the enum-CHECK
    pattern.
    """
    if not _sqlite_table_exists(conn, "order_case"):
        conn.executescript(
            """
            CREATE TABLE order_case (
                case_id                  TEXT PRIMARY KEY,
                tenant_id                TEXT NOT NULL,
                customer_id              TEXT,
                source                   TEXT NOT NULL,
                source_channel           TEXT NOT NULL,
                customer_po_number       TEXT,
                sales_order_id           TEXT,
                edi_transaction_id       TEXT,
                source_email_id          TEXT,
                opened_at                TEXT NOT NULL,
                closed_at                TEXT,
                status                   TEXT NOT NULL DEFAULT 'OPEN_AGENT_PROCESSING',
                sla_deadline             TEXT,
                tier                     INTEGER NOT NULL DEFAULT 2,
                working_memory_summary   TEXT,
                last_compaction_at       TEXT,
                bundle_version_at_open   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_order_case_tenant
                ON order_case (tenant_id);
            CREATE INDEX IF NOT EXISTS idx_order_case_status
                ON order_case (status);
            CREATE INDEX IF NOT EXISTS idx_order_case_sla
                ON order_case (sla_deadline);
            """
        )
    if not _sqlite_column_exists(conn, "exceptions", "parent_case_id"):
        conn.execute(
            "ALTER TABLE exceptions ADD COLUMN parent_case_id TEXT REFERENCES order_case(case_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_exception_parent_case ON exceptions (parent_case_id)"
        )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V009", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    logger.info("SQLite schema V009 applied (order_case parent entity)")


def _apply_sqlite_v010(conn: sqlite3.Connection) -> None:
    """V010 — case_correlation_keys table (ADR-038 Phase H.2).

    Mirrors V010__case_correlation_keys.sql. Previously the SQLite path
    jumped V009 → V012, so the correlation table never existed on the
    test backend — which meant a DB-backed CaseStore (Phase H.7) could
    not be exercised in CI at all. Added so the DB-backed case path is
    testable on SQLite. The PK (tenant_id, key_type, key_value) and the
    case_id FK match the Postgres DDL; key_type vocabulary stays an
    application-layer Literal (V005 enum-decoupling convention).
    """
    if not _sqlite_table_exists(conn, "case_correlation_keys"):
        conn.executescript(
            """
            CREATE TABLE case_correlation_keys (
                tenant_id      TEXT NOT NULL,
                key_type       TEXT NOT NULL,
                key_value      TEXT NOT NULL,
                case_id        TEXT NOT NULL REFERENCES order_case(case_id),
                registered_at  TEXT NOT NULL,
                PRIMARY KEY (tenant_id, key_type, key_value)
            );
            CREATE INDEX IF NOT EXISTS idx_case_corr_case_id
                ON case_correlation_keys (case_id);
            """
        )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V010", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    logger.info("SQLite schema V010 applied (case correlation keys)")


def _apply_sqlite_v014(conn: sqlite3.Connection) -> None:
    """V014 — order_case.updated_at (issue #133 PO #17).

    Mirrors V014__order_case_updated_at.sql. Was missing from the SQLite
    path (V009 → V012 skip), so order_case had no updated_at column on
    the test backend — the CaseStore bumps it on every mutation.
    """
    if not _sqlite_column_exists(conn, "order_case", "updated_at"):
        conn.execute("ALTER TABLE order_case ADD COLUMN updated_at TEXT")
        conn.execute(
            "UPDATE order_case SET updated_at = opened_at WHERE updated_at IS NULL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_order_case_updated ON order_case (updated_at)"
        )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V014", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    logger.info("SQLite schema V014 applied (order_case.updated_at)")


def _apply_sqlite_v021(conn: sqlite3.Connection) -> None:
    """V021 — order_case.pending_override durable cosign state (Phase H.7).

    Mirrors V021__order_case_pending_override.sql. SQLite has no JSONB;
    the column is TEXT and the repository json.dumps/json.loads it (same
    pattern as enrichment_context / resolution_data). NULL = no override.
    """
    if not _sqlite_column_exists(conn, "order_case", "pending_override"):
        conn.execute("ALTER TABLE order_case ADD COLUMN pending_override TEXT")
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V021", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    logger.info("SQLite schema V021 applied (order_case.pending_override)")


def _apply_sqlite_v012(conn: sqlite3.Connection) -> None:
    """V012 — case_events replay log (ADR-038 Phase H.5)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS case_events (
            event_id    TEXT PRIMARY KEY,
            case_id     TEXT NOT NULL,
            tenant_id   TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            tool_name   TEXT NOT NULL,
            tool_call   TEXT NOT NULL,
            tool_result TEXT NOT NULL,
            outcome     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_case_events_case_time
            ON case_events (case_id, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_case_events_tenant_time
            ON case_events (tenant_id, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_case_events_tool
            ON case_events (tenant_id, tool_name);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V012", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    logger.info("SQLite schema V012 applied (case_events replay log)")


def _apply_sqlite_v013(conn: sqlite3.Connection) -> None:
    """V013 — case_locks cross-pod mutex (ADR-038 Phase H.5)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS case_locks (
            case_id     TEXT PRIMARY KEY,
            tenant_id   TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            acquired_by TEXT,
            expires_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_case_locks_expires
            ON case_locks (expires_at);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V013", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    logger.info("SQLite schema V013 applied (case_locks cross-pod mutex)")


def _apply_sqlite_v015(conn: sqlite3.Connection) -> None:
    """V015 — effect_outbox durable ledger (DoR #6)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS effect_outbox (
            id                 TEXT PRIMARY KEY,
            tenant_id          TEXT NOT NULL,
            trace_id           TEXT,
            recipe             TEXT,
            gateway            TEXT NOT NULL,
            operation          TEXT NOT NULL,
            status             TEXT NOT NULL,
            recipe_status      TEXT,
            committed          INTEGER NOT NULL DEFAULT 0,
            needs_compensation INTEGER NOT NULL DEFAULT 0,
            error              TEXT,
            params             TEXT NOT NULL DEFAULT '{}',
            attempts           INTEGER NOT NULL DEFAULT 0,
            escalated          INTEGER NOT NULL DEFAULT 0,
            compensated        INTEGER NOT NULL DEFAULT 0,
            compensated_at     TEXT,
            created_at         TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_effect_outbox_pending
            ON effect_outbox (tenant_id, needs_compensation, compensated, escalated);
        CREATE INDEX IF NOT EXISTS idx_effect_outbox_tenant_time
            ON effect_outbox (tenant_id, created_at);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V015", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    logger.info("SQLite schema V015 applied (effect_outbox ledger)")


def _apply_sqlite_v016(conn: sqlite3.Connection) -> None:
    """V016 — email_attachment store (DoR #10 production attachment store)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS email_attachment (
            id          TEXT PRIMARY KEY,
            tenant_id   TEXT NOT NULL,
            case_id     TEXT,
            name        TEXT NOT NULL,
            mime_type   TEXT NOT NULL,
            size_bytes  INTEGER NOT NULL,
            sha256      TEXT NOT NULL,
            content     BLOB NOT NULL,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_email_attachment_case
            ON email_attachment (tenant_id, case_id);
        CREATE INDEX IF NOT EXISTS idx_email_attachment_tenant_time
            ON email_attachment (tenant_id, created_at);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V016", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    logger.info("SQLite schema V016 applied (email_attachment store)")


def _seed_taxonomy_postgres(cur: Any, data: dict[str, Any]) -> None:
    """Idempotent Postgres seed loader for V017.

    Mirrors scripts.seed_taxonomy.load_taxonomy_sqlite but uses %s
    parameters and Postgres ON CONFLICT syntax. Kept inline (rather than
    in scripts/) so the migration runner is self-contained and does not
    depend on shell execution of the script.
    """
    today = datetime.now(timezone.utc).date().isoformat()

    for sg in data["supergroups"]:
        cur.execute(
            """
            INSERT INTO case_supergroup
                (code, origin, description, owner_role, is_active,
                 effective_from, deprecated_at, replaced_by_code, sort_order, version)
            VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s, %s, 1)
            ON CONFLICT (code) DO UPDATE SET
                origin = EXCLUDED.origin,
                description = EXCLUDED.description,
                owner_role = EXCLUDED.owner_role,
                sort_order = EXCLUDED.sort_order,
                deprecated_at = EXCLUDED.deprecated_at,
                replaced_by_code = EXCLUDED.replaced_by_code,
                version = case_supergroup.version + 1
            """,
            (
                sg["code"], sg["origin"], sg["description"], sg["owner_role"],
                today, sg.get("deprecated_at"), sg.get("replaced_by_code"),
                sg["sort_order"],
            ),
        )

    for intent in data["intents"]:
        cur.execute(
            """
            INSERT INTO case_intent
                (code, supergroup_code, description, sap_block_code, sap_block_field,
                 sap_sales_org, is_active, effective_from, deprecated_at, replaced_by_code)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s, %s, %s)
            ON CONFLICT (code) DO UPDATE SET
                supergroup_code = EXCLUDED.supergroup_code,
                description = EXCLUDED.description,
                sap_block_code = EXCLUDED.sap_block_code,
                sap_block_field = EXCLUDED.sap_block_field,
                sap_sales_org = EXCLUDED.sap_sales_org,
                deprecated_at = EXCLUDED.deprecated_at,
                replaced_by_code = EXCLUDED.replaced_by_code
            """,
            (
                intent["code"], intent["supergroup_code"], intent["description"],
                intent.get("sap_block_code"), intent.get("sap_block_field"),
                intent.get("sap_sales_org"), today,
                intent.get("deprecated_at"), intent.get("replaced_by_code"),
            ),
        )
        cur.execute(
            """
            INSERT INTO supergroup_intent_allowed
                (supergroup_code, intent_code, effective_from, deprecated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (supergroup_code, intent_code) DO UPDATE SET
                deprecated_at = EXCLUDED.deprecated_at
            """,
            (
                intent["supergroup_code"], intent["code"], today,
                intent.get("deprecated_at"),
            ),
        )

    for label in data["labels"]:
        cur.execute(
            """
            INSERT INTO intent_label
                (code, domain, locale, display_name, description)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (code, domain, locale) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                description = EXCLUDED.description
            """,
            (
                label["code"], label["domain"], label.get("locale", "en"),
                label["display_name"], label.get("description"),
            ),
        )


def _apply_sqlite_v017(conn: sqlite3.Connection) -> None:
    """V017 — Case & Intent Super-Group taxonomy lookup tables.

    Mirrors V017__case_taxonomy.sql. Seeds rows by delegating to
    ``scripts.seed_taxonomy.load_taxonomy_sqlite`` so the YAML at
    ``db/seeds/case_taxonomy.yaml`` is the single source of truth on
    both Postgres and SQLite. Plan §3, decision D1.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS case_supergroup (
            code              TEXT PRIMARY KEY,
            origin            TEXT NOT NULL CHECK (origin IN ('CUSTOMER','API','BOTH')),
            description       TEXT NOT NULL,
            owner_role        TEXT NOT NULL,
            is_active         INTEGER NOT NULL DEFAULT 1,
            effective_from    TEXT NOT NULL,
            deprecated_at     TEXT,
            replaced_by_code  TEXT REFERENCES case_supergroup(code),
            sort_order        INTEGER NOT NULL DEFAULT 0,
            version           INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_case_supergroup_origin_active
            ON case_supergroup (origin, is_active);

        CREATE TABLE IF NOT EXISTS case_intent (
            code              TEXT PRIMARY KEY,
            supergroup_code   TEXT NOT NULL REFERENCES case_supergroup(code),
            description       TEXT NOT NULL,
            sap_block_code    TEXT,
            sap_block_field   TEXT
                CHECK (sap_block_field IS NULL OR sap_block_field IN
                       ('LIFSK','LIFSP','FAKSK','FAKSP','ABGRU','CMGST','Z_CUSTOM')),
            sap_sales_org     TEXT,
            is_active         INTEGER NOT NULL DEFAULT 1,
            effective_from    TEXT NOT NULL,
            deprecated_at     TEXT,
            replaced_by_code  TEXT REFERENCES case_intent(code)
        );
        CREATE INDEX IF NOT EXISTS idx_case_intent_supergroup
            ON case_intent (supergroup_code, is_active);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_case_intent_sap_block_active
            ON case_intent (sap_block_code, COALESCE(sap_sales_org, ''))
            WHERE sap_block_code IS NOT NULL AND is_active = 1;

        CREATE TABLE IF NOT EXISTS supergroup_intent_allowed (
            supergroup_code   TEXT NOT NULL REFERENCES case_supergroup(code),
            intent_code       TEXT NOT NULL REFERENCES case_intent(code),
            effective_from    TEXT NOT NULL,
            deprecated_at     TEXT,
            PRIMARY KEY (supergroup_code, intent_code)
        );

        CREATE TABLE IF NOT EXISTS intent_label (
            code              TEXT NOT NULL,
            domain            TEXT NOT NULL CHECK (domain IN ('SUPERGROUP','INTENT')),
            locale            TEXT NOT NULL DEFAULT 'en',
            display_name      TEXT NOT NULL,
            description       TEXT,
            PRIMARY KEY (code, domain, locale)
        );
        CREATE INDEX IF NOT EXISTS idx_intent_label_locale
            ON intent_label (locale, domain);
        """
    )
    # Seed rows from the YAML SoT. Import here to keep the runner
    # importable even when scripts/ is not on sys.path during early test
    # collection.
    from scripts.seed_taxonomy import load_taxonomy_sqlite, load_yaml
    load_taxonomy_sqlite(conn, load_yaml())
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V017", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    logger.info("SQLite schema V017 applied (case taxonomy lookup tables + seed)")


def _apply_sqlite_v018(conn: sqlite3.Connection) -> None:
    """V018 — case origin + supergroup_code + leaf intent_code.

    Mirrors V018__case_origin_supergroup.sql. Adds nullable columns to
    order_case and exceptions, the app_config table, and the inheritance
    + leaf-validity triggers. Backfill of legacy data and NOT NULL
    transitions land in a later migration so this one is reversible.

    SQLite trigger differences vs Postgres:
      - Logic inlined in CREATE TRIGGER (no CREATE FUNCTION).
      - Uses RAISE(ABORT, ...) in place of plpgsql RAISE EXCEPTION.
      - The Postgres `BEFORE UPDATE OF column` form is not supported;
        the WHEN clause filters to the relevant change.
    """
    # 1. app_config
    if not _sqlite_table_exists(conn, "app_config"):
        conn.executescript(
            """
            CREATE TABLE app_config (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                description TEXT,
                updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
    conn.execute(
        """
        INSERT INTO app_config (key, value, description)
        VALUES ('inheritance_mode_customer', 'STRICT',
                'v1 default. Steward may set to RELAXED per §8.1 with no code deploy.')
        ON CONFLICT (key) DO NOTHING
        """
    )

    # 2. order_case columns
    for col, ddl in (
        ("origin",              "ALTER TABLE order_case ADD COLUMN origin TEXT CHECK (origin IS NULL OR origin IN ('CUSTOMER','API'))"),
        ("supergroup_code",     "ALTER TABLE order_case ADD COLUMN supergroup_code TEXT REFERENCES case_supergroup(code)"),
        ("predecessor_case_id", "ALTER TABLE order_case ADD COLUMN predecessor_case_id TEXT REFERENCES order_case(case_id)"),
        ("will_miss_rdd",       "ALTER TABLE order_case ADD COLUMN will_miss_rdd INTEGER NOT NULL DEFAULT 0"),
        ("sla_due_at",          "ALTER TABLE order_case ADD COLUMN sla_due_at TEXT"),
    ):
        if not _sqlite_column_exists(conn, "order_case", col):
            conn.execute(ddl)

    # 3. exceptions columns
    for col, ddl in (
        ("supergroup_code",   "ALTER TABLE exceptions ADD COLUMN supergroup_code TEXT REFERENCES case_supergroup(code)"),
        ("divergence_reason", "ALTER TABLE exceptions ADD COLUMN divergence_reason TEXT"),
        ("intent_code",       "ALTER TABLE exceptions ADD COLUMN intent_code TEXT REFERENCES case_intent(code)"),
        ("sap_block_field",   "ALTER TABLE exceptions ADD COLUMN sap_block_field TEXT CHECK (sap_block_field IS NULL OR sap_block_field IN ('LIFSK','LIFSP','FAKSK','FAKSP','ABGRU','CMGST','Z_CUSTOM'))"),
        ("scope",             "ALTER TABLE exceptions ADD COLUMN scope TEXT CHECK (scope IS NULL OR scope IN ('HEADER','ITEM'))"),
    ):
        if not _sqlite_column_exists(conn, "exceptions", col):
            conn.execute(ddl)

    # 4. + 5. Triggers (inheritance + leaf validity), unified as inline SQLite.
    # One trigger per (event × column-filter). We collapse the inheritance
    # logic into a single CASE chain that RAISEs on the first violated rule.
    #
    # NULL-equality idiom note (review finding #6): the UPDATE triggers use
    # ``NEW.supergroup_code != COALESCE(OLD.supergroup_code, '')`` because
    # SQLite has no ``IS DISTINCT FROM`` operator before 3.39 and we want a
    # NULL→non-NULL transition to count as a change. The empty-string
    # sentinel collides only with a literal empty-string SG code, which the
    # FK to ``case_supergroup(code)`` already rejects.
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS tg_child_supergroup_inheritance_ins;
        CREATE TRIGGER tg_child_supergroup_inheritance_ins
        BEFORE INSERT ON exceptions
        WHEN NEW.parent_case_id IS NOT NULL
         AND NEW.supergroup_code IS NOT NULL
         AND NEW.supergroup_code !=
             COALESCE((SELECT supergroup_code FROM order_case
                       WHERE case_id = NEW.parent_case_id), NEW.supergroup_code)
        BEGIN
            SELECT CASE
                WHEN (SELECT origin FROM order_case WHERE case_id = NEW.parent_case_id) = 'API'
                    THEN RAISE(ABORT, 'API child cases cannot diverge from parent supergroup')
                WHEN COALESCE((SELECT value FROM app_config
                               WHERE key = 'inheritance_mode_customer'), 'STRICT') = 'STRICT'
                    THEN RAISE(ABORT, 'CUSTOMER child cases cannot diverge under STRICT inheritance')
                WHEN NEW.divergence_reason IS NULL
                    THEN RAISE(ABORT, 'divergence_reason required under RELAXED mode when supergroup differs')
            END;
        END;

        DROP TRIGGER IF EXISTS tg_child_supergroup_inheritance_upd;
        CREATE TRIGGER tg_child_supergroup_inheritance_upd
        BEFORE UPDATE ON exceptions
        WHEN NEW.parent_case_id IS NOT NULL
         AND NEW.supergroup_code IS NOT NULL
         AND NEW.supergroup_code != COALESCE(OLD.supergroup_code, '')
         AND NEW.supergroup_code !=
             COALESCE((SELECT supergroup_code FROM order_case
                       WHERE case_id = NEW.parent_case_id), NEW.supergroup_code)
        BEGIN
            SELECT CASE
                WHEN (SELECT origin FROM order_case WHERE case_id = NEW.parent_case_id) = 'API'
                    THEN RAISE(ABORT, 'API child cases cannot diverge from parent supergroup')
                WHEN COALESCE((SELECT value FROM app_config
                               WHERE key = 'inheritance_mode_customer'), 'STRICT') = 'STRICT'
                    THEN RAISE(ABORT, 'CUSTOMER child cases cannot diverge under STRICT inheritance')
                WHEN NEW.divergence_reason IS NULL
                    THEN RAISE(ABORT, 'divergence_reason required under RELAXED mode when supergroup differs')
            END;
        END;

        DROP TRIGGER IF EXISTS tg_child_leaf_validity_ins;
        CREATE TRIGGER tg_child_leaf_validity_ins
        BEFORE INSERT ON exceptions
        WHEN NEW.intent_code IS NOT NULL AND NEW.supergroup_code IS NOT NULL
        BEGIN
            SELECT CASE
                WHEN NOT EXISTS (
                    SELECT 1 FROM supergroup_intent_allowed
                    WHERE supergroup_code = NEW.supergroup_code
                      AND intent_code = NEW.intent_code
                      AND (deprecated_at IS NULL OR deprecated_at > date('now'))
                )
                THEN RAISE(ABORT, 'leaf intent not allowed under supergroup per supergroup_intent_allowed')
            END;
        END;

        DROP TRIGGER IF EXISTS tg_child_leaf_validity_upd;
        CREATE TRIGGER tg_child_leaf_validity_upd
        BEFORE UPDATE ON exceptions
        WHEN NEW.intent_code IS NOT NULL AND NEW.supergroup_code IS NOT NULL
         AND (NEW.intent_code != COALESCE(OLD.intent_code, '')
              OR NEW.supergroup_code != COALESCE(OLD.supergroup_code, ''))
        BEGIN
            SELECT CASE
                WHEN NOT EXISTS (
                    SELECT 1 FROM supergroup_intent_allowed
                    WHERE supergroup_code = NEW.supergroup_code
                      AND intent_code = NEW.intent_code
                      AND (deprecated_at IS NULL OR deprecated_at > date('now'))
                )
                THEN RAISE(ABORT, 'leaf intent not allowed under supergroup per supergroup_intent_allowed')
            END;
        END;

        CREATE INDEX IF NOT EXISTS idx_order_case_supergroup
            ON order_case (supergroup_code);
        CREATE INDEX IF NOT EXISTS idx_order_case_origin
            ON order_case (origin);
        CREATE INDEX IF NOT EXISTS idx_exceptions_intent_code
            ON exceptions (intent_code);
        CREATE INDEX IF NOT EXISTS idx_exceptions_supergroup
            ON exceptions (supergroup_code);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V018", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    logger.info(
        "SQLite schema V018 applied (case origin + supergroup_code + leaf intent_code)"
    )


def _apply_sqlite_v019(conn: sqlite3.Connection) -> None:
    """V019 — drop deprecated case columns. Mirrors V019_*.sql.

    SQLite 3.35+ supports ``ALTER TABLE DROP COLUMN``; the project pins
    Python 3.11+ (SQLite >= 3.40). Idempotent via _sqlite_column_exists.
    """
    for col in ("source", "case_type", "email_classification"):
        if _sqlite_column_exists(conn, "order_case", col):
            conn.execute(f"ALTER TABLE order_case DROP COLUMN {col}")
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V019", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    logger.info("SQLite schema V019 applied (dropped deprecated case columns)")


def _apply_sqlite_v020(conn: sqlite3.Connection) -> None:
    """V020 — case_classification_history (append-only audit trail).

    Mirrors V020__case_classification_history.sql. SQLite triggers use
    RAISE(ABORT, ...) instead of plpgsql RAISE EXCEPTION; behaviour is
    identical — UPDATE and DELETE on this table are rejected.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS case_classification_history (
            id                TEXT PRIMARY KEY,
            tenant_id         TEXT NOT NULL,
            case_id           TEXT NOT NULL REFERENCES order_case(case_id),
            child_case_id     TEXT REFERENCES exceptions(id),
            supergroup_code   TEXT NOT NULL REFERENCES case_supergroup(code),
            intent_code       TEXT REFERENCES case_intent(code),
            classified_at     TEXT NOT NULL,
            classified_by     TEXT NOT NULL,
            classifier_type   TEXT NOT NULL
                CHECK (classifier_type IN ('HUMAN','MODEL','RULE')),
            model_version     TEXT,
            reason_text       TEXT,
            source_event_id   TEXT,
            taxonomy_version  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_case_classification_history_case_time
            ON case_classification_history (case_id, classified_at);
        CREATE INDEX IF NOT EXISTS idx_case_classification_history_tenant_time
            ON case_classification_history (tenant_id, classified_at);
        CREATE INDEX IF NOT EXISTS idx_case_classification_history_child_time
            ON case_classification_history (child_case_id, classified_at);
        CREATE INDEX IF NOT EXISTS idx_case_classification_history_classifier
            ON case_classification_history (classifier_type, classified_at);

        DROP TRIGGER IF EXISTS tg_cch_no_update;
        CREATE TRIGGER tg_cch_no_update
        BEFORE UPDATE ON case_classification_history
        BEGIN
            SELECT RAISE(ABORT, 'case_classification_history is append-only; UPDATE rejected');
        END;

        DROP TRIGGER IF EXISTS tg_cch_no_delete;
        CREATE TRIGGER tg_cch_no_delete
        BEFORE DELETE ON case_classification_history
        BEGIN
            SELECT RAISE(ABORT, 'case_classification_history is append-only; DELETE rejected');
        END;

        -- SQLite quirk: ``INSERT OR REPLACE`` removes the conflicting row
        -- *without* firing BEFORE DELETE (verified locally). Without this
        -- guard, an attacker holding INSERT privilege could overwrite an
        -- existing audit row by issuing ``INSERT OR REPLACE``. The BEFORE
        -- INSERT trigger fires for all insert paths (including OR REPLACE),
        -- so it catches the conflict before the silent-delete happens.
        DROP TRIGGER IF EXISTS tg_cch_no_replace;
        CREATE TRIGGER tg_cch_no_replace
        BEFORE INSERT ON case_classification_history
        WHEN EXISTS (SELECT 1 FROM case_classification_history WHERE id = NEW.id)
        BEGIN
            SELECT RAISE(ABORT, 'case_classification_history is append-only; INSERT OR REPLACE on existing row rejected');
        END;
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V020", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    logger.info(
        "SQLite schema V020 applied (case_classification_history append-only)",
    )


def apply_sqlite(conn: sqlite3.Connection) -> None:
    """Apply the SQLite-compatible schema (V001 + subsequent migrations)."""
    conn.executescript(_SQLITE_SCHEMA)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V001", now),
    )
    conn.commit()
    logger.info("SQLite schema V001 applied")
    _apply_sqlite_v002(conn)
    _apply_sqlite_v003(conn)
    _apply_sqlite_v004(conn)
    # V005 (drop intent CHECK on Postgres) is a no-op on SQLite — the
    # SQLite path never had the constraint. Still record it so version
    # bookkeeping stays in sync across backends.
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V005", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    _apply_sqlite_v006(conn)
    _apply_sqlite_v007(conn)
    _apply_sqlite_v008(conn)
    _apply_sqlite_v009(conn)
    _apply_sqlite_v010(conn)
    _apply_sqlite_v014(conn)
    # V011 (orphan-case backfill) is Postgres-only and deliberately not
    # mirrored: fresh test DBs have no orphan rows, and its SQL uses
    # Postgres-specific sha256/bytea/interval. Record the version so
    # backend bookkeeping stays in sync.
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        ("V011", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    _apply_sqlite_v012(conn)
    _apply_sqlite_v013(conn)
    _apply_sqlite_v015(conn)
    _apply_sqlite_v016(conn)
    _apply_sqlite_v017(conn)
    _apply_sqlite_v018(conn)
    _apply_sqlite_v019(conn)
    _apply_sqlite_v020(conn)
    _apply_sqlite_v021(conn)


def apply_postgres(database_url: str) -> None:
    """Apply PostgreSQL migrations from SQL files.

    Requires ``psycopg2`` or ``psycopg`` to be installed.
    """
    try:
        import psycopg2  # type: ignore[import-untyped]
        conn = psycopg2.connect(database_url)
    except ImportError:
        try:
            import psycopg  # type: ignore[import-untyped]
            conn = psycopg.connect(database_url)
        except ImportError:
            raise RuntimeError(
                "PostgreSQL driver not found. Install psycopg2-binary or psycopg."
            )

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'schema_migrations'
            )
        """)
        table_exists = cur.fetchone()[0]

        v001_applied = False
        if table_exists:
            cur.execute(
                "SELECT version FROM schema_migrations WHERE version = %s",
                ("V001",),
            )
            if cur.fetchone():
                v001_applied = True

        if not v001_applied:
            sql_path = _MIGRATIONS_DIR / "V001__initial_schema.sql"
            sql = sql_path.read_text()
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)",
                ("V001",),
            )
            logger.info("PostgreSQL schema V001 applied")
        else:
            logger.info("PostgreSQL schema V001 already applied, skipping")

        for version, filename, log_msg in (
            ("V002", "V002__reanalyze_columns.sql", "PostgreSQL schema V002 applied"),
            ("V003", "V003__audit_hash_chain.sql", "PostgreSQL schema V003 applied (hash-chained audit log)"),
            ("V004", "V004__enrichment_context.sql", "PostgreSQL schema V004 applied (enrichment_context column)"),
            ("V005", "V005__drop_intent_check.sql", "PostgreSQL schema V005 applied (dropped intent CHECK)"),
            ("V006", "V006__tenant_config.sql", "PostgreSQL schema V006 applied (tenant_config table)"),
            ("V007", "V007__duplicate_po_metadata_contract.sql", "PostgreSQL schema V007 applied (DUPLICATE_PO metadata-contract trigger)"),
            ("V008", "V008__backfill_line_items.sql", "PostgreSQL schema V008 applied (backfilled resolution_data.line_items for legacy rows)"),
            ("V009", "V009__order_case.sql", "PostgreSQL schema V009 applied (order_case parent entity)"),
            ("V010", "V010__case_correlation_keys.sql", "PostgreSQL schema V010 applied (case correlation keys)"),
            ("V011", "V011__backfill_orphan_cases.sql", "PostgreSQL schema V011 applied (orphan case backfill)"),
            ("V012", "V012__case_events.sql", "PostgreSQL schema V012 applied (case_events replay log)"),
            ("V013", "V013__case_locks.sql", "PostgreSQL schema V013 applied (case_locks cross-pod mutex)"),
            ("V014", "V014__order_case_updated_at.sql", "PostgreSQL schema V014 applied (order_case.updated_at)"),
            ("V015", "V015__effect_outbox.sql", "PostgreSQL schema V015 applied (effect_outbox ledger)"),
            ("V016", "V016__email_attachment.sql", "PostgreSQL schema V016 applied (email_attachment store)"),
            ("V017", "V017__case_taxonomy.sql", "PostgreSQL schema V017 applied (case taxonomy lookup tables)"),
            ("V018", "V018__case_origin_supergroup.sql", "PostgreSQL schema V018 applied (case origin + supergroup_code + leaf intent_code)"),
            ("V019", "V019__drop_legacy_case_columns.sql", "PostgreSQL schema V019 applied (dropped deprecated case columns)"),
            ("V020", "V020__case_classification_history.sql", "PostgreSQL schema V020 applied (case classification history append-only)"),
            ("V021", "V021__order_case_pending_override.sql", "PostgreSQL schema V021 applied (order_case.pending_override durable cosign state)"),
        ):
            cur.execute(
                "SELECT version FROM schema_migrations WHERE version = %s",
                (version,),
            )
            if cur.fetchone():
                logger.info("PostgreSQL schema %s already applied, skipping", version)
                continue
            sql_path = _MIGRATIONS_DIR / filename
            cur.execute(sql_path.read_text())
            cur.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)",
                (version,),
            )
            logger.info(log_msg)
            # V017 schema is DDL-only; seed rows are loaded from the YAML
            # SoT so Postgres and SQLite stay byte-identical (D1).
            if version == "V017":
                from scripts.seed_taxonomy import load_yaml
                _seed_taxonomy_postgres(cur, load_yaml())
                logger.info("PostgreSQL V017 taxonomy seed loaded from YAML")

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_migrations(database_url: str) -> None:
    """Auto-detect database type and apply appropriate migrations."""
    if database_url.startswith("sqlite"):
        db_path = database_url.replace("sqlite:///", "").replace("sqlite://", "")
        if db_path == ":memory:" or db_path == "":
            conn = sqlite3.connect(":memory:")
        else:
            conn = sqlite3.connect(db_path)
        try:
            apply_sqlite(conn)
        finally:
            conn.close()
    else:
        apply_postgres(database_url)


if __name__ == "__main__":
    import os
    import sys

    logging.basicConfig(level=logging.INFO)
    url = os.getenv("DATABASE_URL", "")
    if not url:
        print("Set DATABASE_URL to apply migrations.")
        print("  PostgreSQL: DATABASE_URL=postgresql://user:pass@host/dbname")
        print("  SQLite:     DATABASE_URL=sqlite:///path/to/db.sqlite3")
        sys.exit(1)
    apply_migrations(url)
