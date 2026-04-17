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
    intent            TEXT CHECK (intent IN (
                        'CONTRACTUAL_CORRECTION', 'CREDIT_BLOCK',
                        'MASS_PRICING_ERROR', 'DUPLICATE_PO', 'UNKNOWN')),
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


# ---------------------------------------------------------------------------
# V002 — promote original_event and reanalysis_history to dedicated columns
# ---------------------------------------------------------------------------
#
# Idempotent: probes pragma_table_info before each ADD COLUMN so re-runs and
# fresh databases alike converge to the same shape. SQLite lacks
# ADD COLUMN IF NOT EXISTS before 3.35, hence the explicit guard.

def _sqlite_column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


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
        # Check if migrations table exists and V001 is already applied
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
            # Read and execute the full migration SQL
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

        # V002 — reanalyze columns. Idempotent via IF NOT EXISTS in SQL.
        cur.execute(
            "SELECT version FROM schema_migrations WHERE version = %s",
            ("V002",),
        )
        if not cur.fetchone():
            v002_sql = (_MIGRATIONS_DIR / "V002__reanalyze_columns.sql").read_text()
            cur.execute(v002_sql)
            cur.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)",
                ("V002",),
            )
            logger.info("PostgreSQL schema V002 applied")
        else:
            logger.info("PostgreSQL schema V002 already applied, skipping")

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
