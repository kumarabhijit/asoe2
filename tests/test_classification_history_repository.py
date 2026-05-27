"""Direct unit tests for ClassificationHistoryRepository (V020 writer).

The DB-backed audit path was previously exercised only via the
in-memory ExceptionStore in tests/test_child_classification_audit.py.
These tests drive the repository's public API directly, against the
SQLite migration chain, covering the validation guards, the FK
behaviour, the append-only triggers, and the shared-transaction
rollback that DatabaseBackedStore.update relies on.

Review finding #5 from the Phase-5/6 review: the repository had zero
direct unit tests despite being the production writer for the V020
audit trail.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

from db.connection import create_adapter
from db.repository import (
    ClassificationHistoryRepository,
    ExceptionRepository,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def adapter(tmp_path):
    db_path = tmp_path / "v020-repo.db"
    a = create_adapter(f"sqlite:///{db_path}")
    a.apply_schema()
    return a


@pytest.fixture
def repo(adapter: SQLiteAdapter) -> ClassificationHistoryRepository:
    return ClassificationHistoryRepository(adapter)


@pytest.fixture
def case_id(adapter: SQLiteAdapter) -> str:
    """Seed one OrderCase so the FK on case_id resolves."""
    cid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with adapter.cursor("t1") as cur:
        cur.execute(
            "INSERT INTO order_case (case_id, tenant_id, source_channel, "
            "opened_at, status, tier, origin, supergroup_code) "
            "VALUES (?, ?, 'email', ?, 'OPEN_AGENT_PROCESSING', 2, "
            "'CUSTOMER', 'SG_NEW_ORDER')",
            (cid, "t1", now),
        )
    return cid


# ---------------------------------------------------------------------------
# Validation guards
# ---------------------------------------------------------------------------

def test_tenant_id_required(repo, case_id):
    """ADR-028 Guard-rail 4 — every audit row carries tenant_id."""
    with pytest.raises(ValueError, match="tenant_id is required"):
        repo.create(
            tenant_id="", case_id=case_id,
            supergroup_code="SG_NEW_ORDER",
            classified_by="u", classifier_type="HUMAN",
            taxonomy_version="v1",
        )


def test_supergroup_code_required(repo, case_id):
    with pytest.raises(ValueError, match="supergroup_code is required"):
        repo.create(
            tenant_id="t1", case_id=case_id,
            supergroup_code="",
            classified_by="u", classifier_type="HUMAN",
            taxonomy_version="v1",
        )


def test_taxonomy_version_required(repo, case_id):
    """The taxonomy_version stamp is the audit's correctness anchor —
    a row without it cannot be reconciled against a historical
    taxonomy state and is rejected."""
    with pytest.raises(ValueError, match="taxonomy_version must be non-empty"):
        repo.create(
            tenant_id="t1", case_id=case_id,
            supergroup_code="SG_NEW_ORDER",
            classified_by="u", classifier_type="HUMAN",
            taxonomy_version="",
        )


def test_classifier_type_enum_enforced(repo, case_id):
    with pytest.raises(ValueError, match="HUMAN \\| MODEL \\| RULE"):
        repo.create(
            tenant_id="t1", case_id=case_id,
            supergroup_code="SG_NEW_ORDER",
            classified_by="u", classifier_type="BOT",
            taxonomy_version="v1",
        )


# ---------------------------------------------------------------------------
# Happy path — row written, read back, structure intact
# ---------------------------------------------------------------------------

def test_create_returns_inserted_row_shape(repo, case_id):
    row = repo.create(
        tenant_id="t1", case_id=case_id,
        supergroup_code="SG_NEW_ORDER",
        intent_code="INT_MANUAL_ORDER_INTAKE",
        classified_by="user:csr-1", classifier_type="HUMAN",
        reason_text="Initial classification",
        taxonomy_version="2026-05-27-v1",
    )
    assert row["case_id"] == case_id
    assert row["tenant_id"] == "t1"
    assert row["supergroup_code"] == "SG_NEW_ORDER"
    assert row["intent_code"] == "INT_MANUAL_ORDER_INTAKE"
    assert row["classifier_type"] == "HUMAN"
    assert row["reason_text"] == "Initial classification"
    assert row["taxonomy_version"] == "2026-05-27-v1"
    assert row["id"]  # generated UUID


def test_list_by_case_returns_append_order(repo, case_id):
    repo.create(
        tenant_id="t1", case_id=case_id, supergroup_code="SG_NEW_ORDER",
        classified_by="u1", classifier_type="HUMAN", taxonomy_version="v1",
    )
    repo.create(
        tenant_id="t1", case_id=case_id, supergroup_code="SG_NEEDS_TRIAGE",
        classified_by="u2", classifier_type="HUMAN", taxonomy_version="v1",
    )
    rows = repo.list_by_case(case_id, tenant_id="t1")
    assert len(rows) == 2
    sgs = [r["supergroup_code"] for r in rows]
    assert sgs == ["SG_NEW_ORDER", "SG_NEEDS_TRIAGE"]


def test_list_by_case_tenant_scoped(repo, adapter, case_id):
    """ADR-028 — list_by_case filters on tenant_id; cross-tenant
    callers see an empty result, not someone else's audit."""
    repo.create(
        tenant_id="t1", case_id=case_id, supergroup_code="SG_NEW_ORDER",
        classified_by="u", classifier_type="HUMAN", taxonomy_version="v1",
    )
    assert len(repo.list_by_case(case_id, tenant_id="t1")) == 1
    assert len(repo.list_by_case(case_id, tenant_id="t2")) == 0


# ---------------------------------------------------------------------------
# Append-only triggers — DB rejects mutation paths
# ---------------------------------------------------------------------------

def test_fk_unknown_supergroup_rejected(repo, case_id):
    """FK to case_supergroup propagates as IntegrityError."""
    with pytest.raises(sqlite3.IntegrityError):
        repo.create(
            tenant_id="t1", case_id=case_id,
            supergroup_code="SG_DOES_NOT_EXIST",
            classified_by="u", classifier_type="HUMAN",
            taxonomy_version="v1",
        )


def test_update_blocked_by_append_only_trigger(repo, adapter, case_id):
    row = repo.create(
        tenant_id="t1", case_id=case_id, supergroup_code="SG_NEW_ORDER",
        classified_by="u", classifier_type="HUMAN", taxonomy_version="v1",
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with adapter.cursor("t1") as cur:
            cur.execute(
                "UPDATE case_classification_history SET reason_text='x' "
                "WHERE id = ?", (row["id"],),
            )


# ---------------------------------------------------------------------------
# Shared-cursor / transaction rollback (review finding #2)
# ---------------------------------------------------------------------------

def test_shared_cursor_rolls_back_on_failure(adapter, case_id):
    """Two writes in one cursor: if the second raises, the first is
    rolled back too. DatabaseBackedStore.update relies on this so a
    failed audit INSERT doesn't leave a half-mutated exception row.

    We simulate a failure by deliberately violating the FK on the
    second write."""
    exc_repo = ExceptionRepository(adapter)
    hist_repo = ClassificationHistoryRepository(adapter)

    # First create an exception row to update.
    created = exc_repo.create(
        tenant_id="t1", order_id="ord1", event_type="TEST",
        trace_id=str(uuid.uuid4()), intent="INT_PRICE_MISMATCH",
    )
    exc_id = created["id"]

    with pytest.raises(sqlite3.IntegrityError):
        with adapter.cursor("t1") as cur:
            # Mutate the exception row inside the cursor.
            exc_repo.update(
                exc_id, "t1",
                intent="INT_MASS_PRICING_ERROR",
                _cursor=cur,
            )
            # Then try to write an audit row that violates the FK
            # (SG_DOES_NOT_EXIST is not in case_supergroup).
            hist_repo.create(
                tenant_id="t1", case_id=case_id,
                child_case_id=exc_id,
                supergroup_code="SG_DOES_NOT_EXIST",  # FK violation
                classified_by="u", classifier_type="HUMAN",
                taxonomy_version="v1",
                _cursor=cur,
            )

    # The exception row's intent must be unchanged (rollback worked).
    fresh = exc_repo.get(exc_id, "t1")
    assert fresh["intent"] == "INT_PRICE_MISMATCH"


def test_shared_cursor_commits_on_success(adapter, case_id):
    """Sibling of the rollback test — when both writes succeed inside
    one cursor, both rows persist."""
    exc_repo = ExceptionRepository(adapter)
    hist_repo = ClassificationHistoryRepository(adapter)

    created = exc_repo.create(
        tenant_id="t1", order_id="ord1", event_type="TEST",
        trace_id=str(uuid.uuid4()), intent="INT_PRICE_MISMATCH",
    )
    exc_id = created["id"]

    with adapter.cursor("t1") as cur:
        exc_repo.update(
            exc_id, "t1",
            intent="INT_MASS_PRICING_ERROR",
            _cursor=cur,
        )
        hist_repo.create(
            tenant_id="t1", case_id=case_id,
            child_case_id=exc_id,
            supergroup_code="SG_NEW_ORDER",
            classified_by="u", classifier_type="HUMAN",
            taxonomy_version="v1",
            _cursor=cur,
        )

    # Both writes persisted.
    fresh = exc_repo.get(exc_id, "t1")
    assert fresh["intent"] == "INT_MASS_PRICING_ERROR"
    history = hist_repo.list_by_case(case_id, tenant_id="t1")
    assert len(history) == 1
    assert history[0]["child_case_id"] == exc_id
