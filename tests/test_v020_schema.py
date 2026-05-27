"""Phase 3 — V020 case_classification_history schema + append-only locks.

Authority: docs/specs/case-intent-supergroup-requirements.md §8.6.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

from db.migrations.runner import apply_sqlite


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_sqlite(conn)
    yield conn
    conn.close()


def _make_case(conn: sqlite3.Connection, *, supergroup: str = "SG_NEW_ORDER",
               origin: str = "CUSTOMER") -> str:
    case_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO order_case (case_id, tenant_id, source_channel, "
        "opened_at, status, tier, origin, supergroup_code) "
        "VALUES (?, 't1', 'email', ?, 'OPEN_AGENT_PROCESSING', 2, ?, ?)",
        (case_id, now, origin, supergroup),
    )
    conn.commit()
    return case_id


def _insert_history(
    conn: sqlite3.Connection, *,
    case_id: str,
    supergroup_code: str = "SG_NEW_ORDER",
    intent_code: str | None = "INT_MANUAL_ORDER_INTAKE",
    child_case_id: str | None = None,
    classifier_type: str = "HUMAN",
    classified_by: str = "user:csr-1",
    taxonomy_version: str = "2026-05-27-v1",
) -> str:
    row_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO case_classification_history (
            id, case_id, child_case_id, supergroup_code, intent_code,
            classified_at, classified_by, classifier_type,
            taxonomy_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row_id, case_id, child_case_id, supergroup_code, intent_code,
         now, classified_by, classifier_type, taxonomy_version),
    )
    conn.commit()
    return row_id


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_v020_recorded(db: sqlite3.Connection):
    cur = db.execute("SELECT version FROM schema_migrations WHERE version = 'V020'")
    assert cur.fetchone() is not None


def test_table_exists_with_expected_columns(db: sqlite3.Connection):
    cur = db.execute("PRAGMA table_info(case_classification_history)")
    cols = {row[1] for row in cur.fetchall()}
    assert {"id", "case_id", "child_case_id", "supergroup_code", "intent_code",
            "classified_at", "classified_by", "classifier_type",
            "model_version", "reason_text", "source_event_id",
            "taxonomy_version"}.issubset(cols)


def test_classifier_type_check_constraint(db: sqlite3.Connection):
    """classifier_type must be one of HUMAN | MODEL | RULE."""
    case_id = _make_case(db)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_history(db, case_id=case_id, classifier_type="BOT")


# ---------------------------------------------------------------------------
# Write semantics
# ---------------------------------------------------------------------------

def test_parent_level_history_row_accepts_null_child(db: sqlite3.Connection):
    case_id = _make_case(db)
    _insert_history(db, case_id=case_id, child_case_id=None)
    row = db.execute(
        "SELECT supergroup_code, intent_code, child_case_id "
        "FROM case_classification_history WHERE case_id = ?",
        (case_id,),
    ).fetchone()
    assert row[0] == "SG_NEW_ORDER"
    assert row[2] is None


def test_fk_unknown_supergroup_rejected(db: sqlite3.Connection):
    case_id = _make_case(db)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_history(db, case_id=case_id, supergroup_code="SG_DOES_NOT_EXIST")


# ---------------------------------------------------------------------------
# Append-only enforcement (criterion #9)
# ---------------------------------------------------------------------------

def test_update_rejected(db: sqlite3.Connection):
    case_id = _make_case(db)
    row_id = _insert_history(db, case_id=case_id)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute(
            "UPDATE case_classification_history SET reason_text = 'tampered' "
            "WHERE id = ?",
            (row_id,),
        )
        db.commit()


def test_delete_rejected(db: sqlite3.Connection):
    case_id = _make_case(db)
    _insert_history(db, case_id=case_id)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute(
            "DELETE FROM case_classification_history WHERE case_id = ?",
            (case_id,),
        )
        db.commit()


def test_reclassification_appends_second_row(db: sqlite3.Connection):
    """Reclassification writes a new row, not an update of the old one."""
    case_id = _make_case(db)
    _insert_history(db, case_id=case_id, intent_code="INT_MANUAL_ORDER_INTAKE",
                    classifier_type="HUMAN", classified_by="user:csr-1")
    _insert_history(db, case_id=case_id, intent_code="INT_UNKNOWN",
                    supergroup_code="SG_NEEDS_TRIAGE",
                    classifier_type="HUMAN", classified_by="user:lead-1")
    count = db.execute(
        "SELECT COUNT(*) FROM case_classification_history WHERE case_id = ?",
        (case_id,),
    ).fetchone()[0]
    assert count == 2


def test_taxonomy_version_stamped(db: sqlite3.Connection):
    case_id = _make_case(db)
    _insert_history(db, case_id=case_id, taxonomy_version="2026-05-27-v1")
    row = db.execute(
        "SELECT taxonomy_version FROM case_classification_history "
        "WHERE case_id = ?",
        (case_id,),
    ).fetchone()
    assert row[0] == "2026-05-27-v1"
