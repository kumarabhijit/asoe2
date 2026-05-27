"""Phase 2 — leaf validity trigger tests.

Acceptance criterion #4: any (supergroup_code, intent_code) pair not
present in supergroup_intent_allowed (for the effective date) is
rejected at insert.
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


def _insert_orphan(
    conn: sqlite3.Connection,
    *,
    supergroup_code: str | None,
    intent_code: str | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO exceptions (
            id, tenant_id, order_id, event_type, trace_id,
            intent, lifecycle_state, created_at, updated_at,
            supergroup_code, intent_code
        ) VALUES (?, 't1', 'ord1', 'TEST', ?, ?, 'INGESTED', ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()), str(uuid.uuid4()),
            intent_code, now, now,
            supergroup_code, intent_code,
        ),
    )
    conn.commit()


def test_valid_pair_inserts(db: sqlite3.Connection):
    """A (sg, intent) pair declared in the YAML is accepted."""
    _insert_orphan(
        db, supergroup_code="SG_BLOCK_PRICING", intent_code="INT_PRICE_MISMATCH"
    )


def test_invalid_pair_rejected(db: sqlite3.Connection):
    """Criterion #4: cross-supergroup leaf is rejected."""
    with pytest.raises(sqlite3.IntegrityError, match="not allowed under supergroup"):
        _insert_orphan(
            db, supergroup_code="SG_BLOCK_PRICING", intent_code="INT_CREDIT_BLOCK"
        )


def test_unknown_intent_rejected_by_fk(db: sqlite3.Connection):
    """Garbage intent code is caught by the FK to case_intent."""
    with pytest.raises(sqlite3.IntegrityError):
        _insert_orphan(
            db, supergroup_code="SG_BLOCK_PRICING", intent_code="INT_GARBAGE_XYZ"
        )


def test_null_intent_skips_trigger(db: sqlite3.Connection):
    """Transitional: a row with supergroup but no leaf yet is allowed."""
    _insert_orphan(db, supergroup_code="SG_BLOCK_PRICING", intent_code=None)


def test_null_supergroup_skips_trigger(db: sqlite3.Connection):
    """Transitional: a row with leaf but no supergroup is allowed (legacy)."""
    _insert_orphan(db, supergroup_code=None, intent_code="INT_PRICE_MISMATCH")


def test_deprecated_pair_rejected(db: sqlite3.Connection):
    """A pair deprecated before today is no longer valid."""
    db.execute(
        """
        UPDATE supergroup_intent_allowed
        SET deprecated_at = date('now', '-1 day')
        WHERE supergroup_code = 'SG_BLOCK_PRICING' AND intent_code = 'INT_PRICE_MISMATCH'
        """
    )
    db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="not allowed under supergroup"):
        _insert_orphan(
            db, supergroup_code="SG_BLOCK_PRICING", intent_code="INT_PRICE_MISMATCH"
        )


def test_update_to_invalid_pair_rejected(db: sqlite3.Connection):
    """The UPDATE trigger fires for late reclassification."""
    _insert_orphan(
        db, supergroup_code="SG_BLOCK_PRICING", intent_code="INT_PRICE_MISMATCH"
    )
    # Try to reclassify intent to a leaf that doesn't belong under SG_BLOCK_PRICING
    with pytest.raises(sqlite3.IntegrityError, match="not allowed under supergroup"):
        db.execute(
            "UPDATE exceptions SET intent_code = 'INT_CREDIT_BLOCK' "
            "WHERE supergroup_code = 'SG_BLOCK_PRICING'"
        )
        db.commit()
