"""Phase H.7 — migration-chain schema locks.

Locks the Increment-1 schema fixes that make a DB-backed CaseStore
possible:

  * The SQLite mirror previously jumped V009 → V012, so
    ``case_correlation_keys`` (V010) and ``order_case.updated_at``
    (V014) never existed on the test backend — a DB-backed case store
    could not be exercised in CI at all.
  * V021 adds ``order_case.pending_override`` (durable cosign state).
  * V009/V011 referenced a non-existent ``exception_record`` table;
    the column belongs on ``exceptions`` (validated here on SQLite and
    by the apply_postgres CI gate on real Postgres).

These assertions fail on the parent commit (the columns/table were
absent / mis-targeted), per the CLAUDE.md bug-fix regression gate.
"""

from __future__ import annotations

import sqlite3

import pytest

from db.migrations.runner import apply_sqlite


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_sqlite(conn)
    yield conn
    conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def test_case_correlation_keys_table_exists(db: sqlite3.Connection) -> None:
    """V010 mirror — the correlation table backs case dedup/lookup."""
    assert _table_exists(db, "case_correlation_keys")
    cols = _columns(db, "case_correlation_keys")
    assert {"tenant_id", "key_type", "key_value", "case_id", "registered_at"} <= cols


def test_case_correlation_pk_rejects_duplicate_key(db: sqlite3.Connection) -> None:
    """PK (tenant_id, key_type, key_value) — one key maps to one case."""
    case_id = "case-corr-1"
    db.execute(
        "INSERT INTO order_case (case_id, tenant_id, source_channel, opened_at, "
        "status, tier) VALUES (?, 't1', 'edi_x12_850', '2026-01-01T00:00:00Z', "
        "'OPEN_AGENT_PROCESSING', 2)",
        (case_id,),
    )
    db.execute(
        "INSERT INTO case_correlation_keys VALUES ('t1','customer_po_number',"
        "'PO-1', ?, '2026-01-01T00:00:00Z')",
        (case_id,),
    )
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO case_correlation_keys VALUES ('t1','customer_po_number',"
            "'PO-1', ?, '2026-01-01T00:00:00Z')",
            (case_id,),
        )


def test_order_case_has_updated_at(db: sqlite3.Connection) -> None:
    """V014 mirror — CaseStore bumps updated_at on every mutation."""
    assert "updated_at" in _columns(db, "order_case")


def test_order_case_has_pending_override(db: sqlite3.Connection) -> None:
    """V021 — durable cosign / four-eyes state."""
    assert "pending_override" in _columns(db, "order_case")


def test_legacy_source_columns_dropped(db: sqlite3.Connection) -> None:
    """V019 — origin replaced source; source must be gone."""
    cols = _columns(db, "order_case")
    assert "origin" in cols
    assert "source" not in cols


def test_exceptions_has_parent_case_id(db: sqlite3.Connection) -> None:
    """V009 (corrected) — child→parent edge on the real `exceptions` table."""
    assert "parent_case_id" in _columns(db, "exceptions")


def test_migration_versions_recorded(db: sqlite3.Connection) -> None:
    """V010/V011/V014/V021 bookkeeping stays in sync across backends."""
    recorded = {
        r[0] for r in db.execute("SELECT version FROM schema_migrations").fetchall()
    }
    assert {"V010", "V011", "V014", "V021"} <= recorded


def test_reapply_is_idempotent(db: sqlite3.Connection) -> None:
    """Re-running the full chain is a no-op (version-guarded)."""
    apply_sqlite(db)  # second apply on the already-migrated conn
    assert "pending_override" in _columns(db, "order_case")
    assert _table_exists(db, "case_correlation_keys")
