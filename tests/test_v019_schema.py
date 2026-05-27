"""Phase 2 — V019 drops the legacy case columns.

Verifies that ``source`` / ``case_type`` / ``email_classification`` are
gone from ``order_case`` after the SQLite migration chain runs.
Authority: docs/specs/case-intent-supergroup-requirements.md §10 + the
PO direction "no production -> no back-compat shims" (27-May).
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


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_legacy_columns_dropped(db: sqlite3.Connection):
    """``source`` / ``case_type`` / ``email_classification`` no longer exist."""
    cols = _cols(db, "order_case")
    assert "source" not in cols
    assert "case_type" not in cols
    assert "email_classification" not in cols


def test_replacement_columns_present(db: sqlite3.Connection):
    """The columns that supplanted them are still there post-V019."""
    cols = _cols(db, "order_case")
    assert "origin" in cols
    assert "supergroup_code" in cols


def test_v019_recorded(db: sqlite3.Connection):
    cur = db.execute("SELECT version FROM schema_migrations WHERE version = 'V019'")
    assert cur.fetchone() is not None


def test_v019_idempotent(db: sqlite3.Connection):
    """Re-applying V019 against an already-dropped schema is a no-op,
    not a crash. The runner's _sqlite_column_exists guard handles this."""
    from db.migrations.runner import _apply_sqlite_v019
    _apply_sqlite_v019(db)
    cols = _cols(db, "order_case")
    assert "source" not in cols
