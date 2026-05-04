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
