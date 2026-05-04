"""V007 — DB-level metadata-contract enforcement tests.

ADR-028 G1 / metadata-contract.md V1.5. The trigger added in V007
rejects DUPLICATE_PO rows missing required resolution_data keys at
INSERT/UPDATE time. These tests verify:

  * Valid DUPLICATE_PO rows write successfully (positive path).
  * Each missing key (signal_breakdown / composite_score /
    classification / recommended_action) raises an IntegrityError
    with the offending field named in the message.
  * Pre-recipe rows (final_status IS NULL) are unaffected — the
    contract only fires once the recipe has produced output.
  * Non-DUPLICATE_PO rows are unaffected — the trigger is intent-
    scoped.
  * UPDATE that would create a violation is also rejected.
  * UPDATE of an unrelated field (e.g. lifecycle_state) on an already-
    valid row passes.

All tests use SQLite in-memory — the SQLite trigger mirror is in
db/migrations/runner.py::_apply_sqlite_v007 and uses
``json_extract`` + ``RAISE(ABORT, ...)`` to match the Postgres
plpgsql semantics.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict

import pytest

from db.connection import SQLiteAdapter
from db.repository import ExceptionRepository


_VALID_DUP_RESOLUTION: Dict[str, Any] = {
    "signal_breakdown": {"po_number": 0.30, "customer_id": 0.15},
    "composite_score": 0.94,
    "classification": "AUTO_BLOCK",
    "recommended_action": "BLOCK_AND_NOTIFY",
}


@pytest.fixture
def adapter() -> SQLiteAdapter:
    a = SQLiteAdapter(":memory:")
    a.apply_schema()
    return a


@pytest.fixture
def repo(adapter: SQLiteAdapter) -> ExceptionRepository:
    return ExceptionRepository(adapter=adapter)


# ---------------------------------------------------------------------------
# Positive path — a complete DUPLICATE_PO row writes
# ---------------------------------------------------------------------------


class TestInsertHappyPath:
    def test_valid_row_inserts(self, repo: ExceptionRepository):
        row = repo.create(
            tenant_id="acme",
            order_id="PO-1",
            event_type="EDI_850_DUPLICATE_PO",
            trace_id="trace-1",
            intent="DUPLICATE_PO",
            final_status="MANUAL_REVIEW_REQUIRED",
            resolution_data=_VALID_DUP_RESOLUTION,
        )
        assert row["id"]
        assert row["resolution_data"] == _VALID_DUP_RESOLUTION


# ---------------------------------------------------------------------------
# Negative path — each missing required key trips the trigger
# ---------------------------------------------------------------------------


class TestInsertEnforcement:
    @pytest.mark.parametrize(
        "missing_key",
        ["signal_breakdown", "composite_score", "classification", "recommended_action"],
    )
    def test_missing_key_rejected(
        self, repo: ExceptionRepository, missing_key: str,
    ):
        bad = dict(_VALID_DUP_RESOLUTION)
        del bad[missing_key]
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            repo.create(
                tenant_id="acme",
                order_id="PO-1",
                event_type="EDI_850_DUPLICATE_PO",
                trace_id="trace-1",
                intent="DUPLICATE_PO",
                final_status="MANUAL_REVIEW_REQUIRED",
                resolution_data=bad,
            )
        # Error message names the offending field for diagnosability
        assert missing_key in str(exc_info.value)

    def test_completely_empty_resolution_rejected(
        self, repo: ExceptionRepository,
    ):
        with pytest.raises(sqlite3.IntegrityError):
            repo.create(
                tenant_id="acme",
                order_id="PO-1",
                event_type="EDI_850_DUPLICATE_PO",
                trace_id="trace-1",
                intent="DUPLICATE_PO",
                final_status="MANUAL_REVIEW_REQUIRED",
                resolution_data={},
            )


# ---------------------------------------------------------------------------
# Scope — trigger does NOT fire on pre-recipe rows or other intents
# ---------------------------------------------------------------------------


class TestTriggerScope:
    def test_pre_recipe_row_unaffected(self, repo: ExceptionRepository):
        """A DUPLICATE_PO row in INGESTED state (final_status=None)
        is allowed to have empty resolution_data — the recipe hasn't
        run yet."""
        row = repo.create(
            tenant_id="acme",
            order_id="PO-1",
            event_type="EDI_850_DUPLICATE_PO",
            trace_id="trace-1",
            intent="DUPLICATE_PO",
            lifecycle_state="INGESTED",
            final_status=None,
            resolution_data={},
        )
        assert row["id"]

    def test_non_duplicate_po_intent_unaffected(
        self, repo: ExceptionRepository,
    ):
        """A CONTRACTUAL_CORRECTION row with the same empty
        resolution_data passes — the contract is intent-scoped."""
        row = repo.create(
            tenant_id="acme",
            order_id="PO-CC-1",
            event_type="EDI_850_PRICE_MISMATCH",
            trace_id="trace-2",
            intent="CONTRACTUAL_CORRECTION",
            final_status="COMPLETE",
            resolution_data={},
        )
        assert row["id"]

    def test_unknown_intent_unaffected(self, repo: ExceptionRepository):
        row = repo.create(
            tenant_id="acme",
            order_id="PO-?-1",
            event_type="UNKNOWN",
            trace_id="trace-3",
            intent="UNKNOWN",
            final_status="FAIL_TO_HUMAN",
            resolution_data={},
        )
        assert row["id"]


# ---------------------------------------------------------------------------
# UPDATE path — trigger fires on UPDATE just like INSERT
# ---------------------------------------------------------------------------


class TestUpdateEnforcement:
    def _seed_valid(self, repo: ExceptionRepository) -> Dict[str, Any]:
        return repo.create(
            tenant_id="acme",
            order_id="PO-1",
            event_type="EDI_850_DUPLICATE_PO",
            trace_id="trace-1",
            intent="DUPLICATE_PO",
            final_status="MANUAL_REVIEW_REQUIRED",
            resolution_data=_VALID_DUP_RESOLUTION,
        )

    def test_update_to_invalid_resolution_rejected(
        self, repo: ExceptionRepository,
    ):
        row = self._seed_valid(repo)
        bad = dict(_VALID_DUP_RESOLUTION)
        del bad["composite_score"]
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            repo.update(row["id"], "acme", resolution_data=bad)
        assert "composite_score" in str(exc_info.value)

    def test_update_unrelated_field_passes(
        self, repo: ExceptionRepository,
    ):
        """Updating only lifecycle_state on a valid row succeeds — the
        trigger sees NEW.resolution_data unchanged (still valid) and
        passes through."""
        row = self._seed_valid(repo)
        updated = repo.update(
            row["id"], "acme", lifecycle_state="RESOLVED",
        )
        assert updated is not None
        assert updated["lifecycle_state"] == "RESOLVED"
        assert updated["resolution_data"] == _VALID_DUP_RESOLUTION

    def test_update_setting_resolution_to_empty_rejected(
        self, repo: ExceptionRepository,
    ):
        """A regression-style call that wipes resolution_data on an
        existing DUPLICATE_PO row is the canonical V1.5 catch."""
        row = self._seed_valid(repo)
        with pytest.raises(sqlite3.IntegrityError):
            repo.update(row["id"], "acme", resolution_data={})
