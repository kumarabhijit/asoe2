"""Phase 2 — V018 schema adds Origin / Super-Group fields + triggers.

Covers requirements §5 (model fields). Schema state observed here is
*after* V019 has chained and dropped the legacy ``source`` /
``case_type`` / ``email_classification`` columns — see
``tests/test_v019_schema.py`` for the drop assertions.
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


def test_order_case_has_new_columns(db: sqlite3.Connection):
    cols = _cols(db, "order_case")
    assert {"origin", "supergroup_code", "predecessor_case_id",
            "will_miss_rdd", "sla_due_at"}.issubset(cols)


def test_exceptions_has_new_columns(db: sqlite3.Connection):
    cols = _cols(db, "exceptions")
    assert {"supergroup_code", "divergence_reason", "intent_code",
            "sap_block_field", "scope"}.issubset(cols)


def test_anchor_columns_present(db: sqlite3.Connection):
    """V009 anchor columns survive V018 + V019."""
    ex = _cols(db, "exceptions")
    assert "parent_case_id" in ex
    assert "intent" in ex  # legacy column; renamed to intent_code later


def test_app_config_seeded_with_strict_default(db: sqlite3.Connection):
    cur = db.execute(
        "SELECT value FROM app_config WHERE key = 'inheritance_mode_customer'"
    )
    assert cur.fetchone()[0] == "STRICT"


def test_v018_recorded(db: sqlite3.Connection):
    cur = db.execute("SELECT version FROM schema_migrations WHERE version = 'V018'")
    assert cur.fetchone() is not None


def test_triggers_created(db: sqlite3.Connection):
    cur = db.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'tg_child_%'"
    )
    names = {row[0] for row in cur.fetchall()}
    assert names == {
        "tg_child_supergroup_inheritance_ins",
        "tg_child_supergroup_inheritance_upd",
        "tg_child_leaf_validity_ins",
        "tg_child_leaf_validity_upd",
    }
