"""Phase 2 — inheritance trigger tests.

Covers acceptance criteria #2 and #3:
  #2 An API parent rejects a child whose supergroup_code differs.
  #3 A CUSTOMER parent under STRICT (v1 default) also rejects divergence.

Also exercises the latent RELAXED capability (§8.1) so we know the trigger
honours the app_config flag — even though v1 ships with STRICT and the
column ``divergence_reason`` stays NULL in production.
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


def _make_case(
    conn: sqlite3.Connection,
    *,
    origin: str,
    supergroup_code: str,
    tenant_id: str = "t1",
) -> str:
    case_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO order_case (
            case_id, tenant_id, source, source_channel,
            opened_at, status, tier, origin, supergroup_code
        ) VALUES (?, ?, ?, ?, ?, 'OPEN_AGENT_PROCESSING', 2, ?, ?)
        """,
        (case_id, tenant_id, "manual_order" if origin == "CUSTOMER" else "automated_order",
         "email" if origin == "CUSTOMER" else "edi_x12_850",
         now, origin, supergroup_code),
    )
    conn.commit()
    return case_id


def _insert_child(
    conn: sqlite3.Connection,
    *,
    parent_case_id: str,
    supergroup_code: str,
    intent_code: str,
    tenant_id: str = "t1",
    divergence_reason: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO exceptions (
            id, tenant_id, order_id, event_type, trace_id,
            intent, lifecycle_state, created_at, updated_at,
            parent_case_id, supergroup_code, intent_code, divergence_reason
        ) VALUES (?, ?, ?, 'TEST', ?, ?, 'INGESTED', ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()), tenant_id, "ord1",
            str(uuid.uuid4()),
            intent_code, now, now,
            parent_case_id, supergroup_code, intent_code, divergence_reason,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# v1 STRICT behaviour
# ---------------------------------------------------------------------------

def test_api_parent_accepts_matching_child(db: sqlite3.Connection):
    parent = _make_case(db, origin="API", supergroup_code="SG_BLOCK_PRICING")
    _insert_child(
        db, parent_case_id=parent,
        supergroup_code="SG_BLOCK_PRICING", intent_code="INT_PRICE_MISMATCH",
    )
    cur = db.execute("SELECT COUNT(*) FROM exceptions WHERE parent_case_id = ?", (parent,))
    assert cur.fetchone()[0] == 1


def test_api_parent_rejects_divergent_child(db: sqlite3.Connection):
    """Criterion #2."""
    parent = _make_case(db, origin="API", supergroup_code="SG_BLOCK_PRICING")
    with pytest.raises(sqlite3.IntegrityError, match="API child cases cannot diverge"):
        _insert_child(
            db, parent_case_id=parent,
            supergroup_code="SG_BLOCK_CREDIT", intent_code="INT_CREDIT_BLOCK",
        )


def test_customer_parent_strict_rejects_divergent_child(db: sqlite3.Connection):
    """Criterion #3 (v1 default STRICT). Divergent pair is itself valid
    (SG_NEEDS_TRIAGE + INT_UNKNOWN both seeded) — we are testing the
    inheritance trigger, not the leaf-validity one."""
    parent = _make_case(db, origin="CUSTOMER", supergroup_code="SG_NEW_ORDER")
    with pytest.raises(sqlite3.IntegrityError, match="STRICT inheritance"):
        _insert_child(
            db, parent_case_id=parent,
            supergroup_code="SG_NEEDS_TRIAGE", intent_code="INT_UNKNOWN",
        )


def test_customer_parent_accepts_matching_child(db: sqlite3.Connection):
    parent = _make_case(db, origin="CUSTOMER", supergroup_code="SG_NEW_ORDER")
    _insert_child(
        db, parent_case_id=parent,
        supergroup_code="SG_NEW_ORDER", intent_code="INT_MANUAL_ORDER_INTAKE",
    )
    cur = db.execute("SELECT COUNT(*) FROM exceptions WHERE parent_case_id = ?", (parent,))
    assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Latent RELAXED capability (§8.1)
# ---------------------------------------------------------------------------

def test_customer_parent_relaxed_with_reason_accepts(db: sqlite3.Connection):
    """Latent RELAXED mode: divergent CUSTOMER child accepted with reason.
    Uses SG_NEEDS_TRIAGE + INT_UNKNOWN as the divergent target — both
    seeded, so leaf validity passes and we are exercising inheritance only."""
    db.execute(
        "UPDATE app_config SET value = 'RELAXED' WHERE key = 'inheritance_mode_customer'"
    )
    db.commit()
    parent = _make_case(db, origin="CUSTOMER", supergroup_code="SG_NEW_ORDER")
    _insert_child(
        db, parent_case_id=parent,
        supergroup_code="SG_NEEDS_TRIAGE", intent_code="INT_UNKNOWN",
        divergence_reason="Triage uncertain — escalating",
    )
    cur = db.execute("SELECT COUNT(*) FROM exceptions WHERE parent_case_id = ?", (parent,))
    assert cur.fetchone()[0] == 1


def test_customer_parent_relaxed_without_reason_rejects(db: sqlite3.Connection):
    """RELAXED still requires divergence_reason."""
    db.execute(
        "UPDATE app_config SET value = 'RELAXED' WHERE key = 'inheritance_mode_customer'"
    )
    db.commit()
    parent = _make_case(db, origin="CUSTOMER", supergroup_code="SG_NEW_ORDER")
    with pytest.raises(sqlite3.IntegrityError, match="divergence_reason required"):
        _insert_child(
            db, parent_case_id=parent,
            supergroup_code="SG_NEEDS_TRIAGE", intent_code="INT_UNKNOWN",
            divergence_reason=None,
        )


def test_api_relaxed_still_rejects_divergence(db: sqlite3.Connection):
    """RELAXED mode applies only to CUSTOMER. API stays strict regardless."""
    db.execute(
        "UPDATE app_config SET value = 'RELAXED' WHERE key = 'inheritance_mode_customer'"
    )
    db.commit()
    parent = _make_case(db, origin="API", supergroup_code="SG_BLOCK_PRICING")
    with pytest.raises(sqlite3.IntegrityError, match="API child cases cannot diverge"):
        _insert_child(
            db, parent_case_id=parent,
            supergroup_code="SG_BLOCK_CREDIT", intent_code="INT_CREDIT_BLOCK",
            divergence_reason="should be ignored on API path",
        )


# ---------------------------------------------------------------------------
# Edge cases (transitional state)
# ---------------------------------------------------------------------------

def test_child_with_null_supergroup_passes(db: sqlite3.Connection):
    """Children that don't yet carry supergroup_code (legacy / transitional)
    must still insert — the trigger no-ops when the value is NULL."""
    parent = _make_case(db, origin="API", supergroup_code="SG_BLOCK_PRICING")
    _insert_child(
        db, parent_case_id=parent,
        supergroup_code=None,  # transitional
        intent_code=None,
    )


def test_child_without_parent_passes(db: sqlite3.Connection):
    """Orphan child cases (no parent_case_id) are allowed — inheritance only
    fires when parent_case_id is set."""
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        INSERT INTO exceptions (
            id, tenant_id, order_id, event_type, trace_id,
            intent, lifecycle_state, created_at, updated_at,
            parent_case_id, supergroup_code, intent_code
        ) VALUES (?, 't1', 'ord1', 'TEST', ?, ?, 'INGESTED', ?, ?,
                  NULL, 'SG_BLOCK_PRICING', 'INT_PRICE_MISMATCH')
        """,
        (str(uuid.uuid4()), str(uuid.uuid4()), "INT_PRICE_MISMATCH", now, now),
    )
    db.commit()
