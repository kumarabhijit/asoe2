"""V008 — Backfill resolution_data.line_items for legacy exception rows.

The migration runs as part of `apply_schema()` so by the time these
tests construct an adapter the migration has already executed once
on whatever rows existed (zero, on a fresh in-memory DB). The tests
seed data BEFORE running the migration manually a second time so we
can observe the behaviour deterministically:

  1. Backfills line_items into a legacy row (no line_items key).
  2. Idempotent — re-running doesn't double-insert / mutate.
  3. Respects already-populated rows (multi-line recipe output stays).
  4. Skips rows with NULL original_event.
  5. V007-compatible — a conformant DUPLICATE_PO row backfills cleanly.
  6. V007-compatible — a non-conformant pre-V007 DUPLICATE_PO row is
     left alone (the migration skips it rather than tripping the
     BEFORE INSERT OR UPDATE trigger).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

import pytest

from db.connection import SQLiteAdapter
from db.migrations.runner import _apply_sqlite_v008


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_row(
    adapter: SQLiteAdapter,
    *,
    intent: Optional[str] = "CONTRACTUAL_CORRECTION",
    final_status: Optional[str] = "MANUAL_REVIEW_REQUIRED",
    resolution_data: Optional[Dict[str, Any]] = None,
    original_event: Optional[Dict[str, Any]] = None,
) -> str:
    """Insert a row directly so the V007 trigger doesn't run during seed.

    The trigger only fires on rows whose ``intent='DUPLICATE_PO'`` AND
    ``final_status IS NOT NULL``; for those, the seeder must supply the
    four audit-bearing keys to avoid the trigger aborting the insert.
    Other intents bypass the trigger trivially.
    """
    rec_id = str(uuid4())
    rd_text = json.dumps(resolution_data) if resolution_data is not None else None
    evt_text = json.dumps(original_event) if original_event is not None else None
    with adapter.cursor("acme") as cur:
        cur.execute(
            """INSERT INTO exceptions (
                   id, tenant_id, order_id, event_type, intent,
                   lifecycle_state, final_status, trace_id,
                   resolution_data, original_event,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rec_id, "acme", "PO-X", "EDI_850", intent,
                "PENDING_REVIEW", final_status, str(uuid4()),
                rd_text, evt_text,
                _now(), _now(),
            ),
        )
    return rec_id


def _read_resolution_data(adapter: SQLiteAdapter, rec_id: str) -> Dict[str, Any]:
    with adapter.cursor("acme") as cur:
        cur.execute(
            "SELECT resolution_data FROM exceptions WHERE id = ?", (rec_id,),
        )
        row = cur.fetchone()
    raw = row[0] if row else None
    return json.loads(raw) if raw else {}


def _run_migration(adapter: SQLiteAdapter) -> None:
    """Run V008 against the adapter's connection."""
    with adapter.connection() as conn:
        _apply_sqlite_v008(conn)


@pytest.fixture
def adapter() -> SQLiteAdapter:
    a = SQLiteAdapter(":memory:")
    a.apply_schema()
    return a


# ---------------------------------------------------------------------------
# Positive path — backfills legacy rows
# ---------------------------------------------------------------------------


class TestBackfillLegacyRows:
    def test_backfills_row_missing_line_items_key(self, adapter):
        rec_id = _seed_row(
            adapter,
            resolution_data={"status": "SUCCESS"},  # no line_items key
            original_event={
                "order_id": "PO-1",
                "line_item": 1,
                "po_price": 90.0,
                "sap_base_price": 100.0,
                "event_type": "EDI_850_PRICE_MISMATCH",
                "line_count": 1,
            },
        )
        _run_migration(adapter)

        rd = _read_resolution_data(adapter, rec_id)
        assert "line_items" in rd
        assert isinstance(rd["line_items"], list)
        assert len(rd["line_items"]) == 1
        item = rd["line_items"][0]
        assert item["line_id"] == "PO-1-1"
        assert item["sku"] == "PO-1"  # falls back to order_id when sku absent
        assert item["description"] == "EDI_850_PRICE_MISMATCH"
        assert item["uom"] == "EA"
        assert item["quantity"] == 1
        assert item["erp_price"] == 100.0
        assert item["po_price"] == 90.0
        # Recipe-supplied keys preserved.
        assert rd["status"] == "SUCCESS"

    def test_backfills_row_with_null_resolution_data(self, adapter):
        rec_id = _seed_row(
            adapter,
            resolution_data=None,
            original_event={
                "order_id": "PO-2",
                "line_item": 1,
                "po_price": 50.0,
                "sap_base_price": 50.0,
                "event_type": "EDI_850",
                "line_count": 3,
            },
        )
        _run_migration(adapter)

        rd = _read_resolution_data(adapter, rec_id)
        assert rd["line_items"][0]["quantity"] == 3
        assert rd["line_items"][0]["po_price"] == 50.0

    def test_uses_sku_when_present_on_event(self, adapter):
        rec_id = _seed_row(
            adapter,
            resolution_data={},
            original_event={
                "order_id": "PO-3",
                "line_item": 1,
                "sku": "WIDGET-A",
                "po_price": 10.0,
                "sap_base_price": 10.0,
                "event_type": "EDI_850",
                "line_count": 1,
            },
        )
        _run_migration(adapter)

        rd = _read_resolution_data(adapter, rec_id)
        assert rd["line_items"][0]["sku"] == "WIDGET-A"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_re_running_migration_does_not_modify(self, adapter):
        rec_id = _seed_row(
            adapter,
            resolution_data={},
            original_event={
                "order_id": "PO-IDEM",
                "line_item": 1,
                "po_price": 1.0,
                "sap_base_price": 1.0,
                "event_type": "X",
                "line_count": 1,
            },
        )
        _run_migration(adapter)
        first = _read_resolution_data(adapter, rec_id)

        _run_migration(adapter)
        _run_migration(adapter)
        third = _read_resolution_data(adapter, rec_id)

        # Same single row, same values — no double-projection.
        assert first == third
        assert len(third["line_items"]) == 1


# ---------------------------------------------------------------------------
# Skip paths
# ---------------------------------------------------------------------------


class TestSkipPaths:
    def test_preserves_recipe_supplied_line_items(self, adapter):
        recipe_lines = [
            {
                "line_id": "RECIPE-1",
                "sku": "RECIPE-SKU",
                "description": "recipe-supplied",
                "uom": "EA",
                "quantity": 7,
                "erp_price": 99.0,
                "po_price": 88.0,
            },
            {
                "line_id": "RECIPE-2",
                "sku": "RECIPE-SKU-2",
                "description": "second line",
                "uom": "CS",
                "quantity": 3,
                "erp_price": 49.0,
                "po_price": 49.0,
            },
        ]
        rec_id = _seed_row(
            adapter,
            resolution_data={"line_items": recipe_lines},
            original_event={
                "order_id": "PO-SHOULD-NOT-OVERWRITE",
                "line_item": 1,
                "po_price": 0.0,
                "sap_base_price": 0.0,
                "event_type": "X",
                "line_count": 1,
            },
        )
        _run_migration(adapter)

        rd = _read_resolution_data(adapter, rec_id)
        assert rd["line_items"] == recipe_lines

    def test_skips_row_with_null_original_event(self, adapter):
        rec_id = _seed_row(
            adapter,
            resolution_data={},
            original_event=None,
        )
        _run_migration(adapter)

        rd = _read_resolution_data(adapter, rec_id)
        # No source to project from; line_items stays absent.
        assert "line_items" not in rd

    def test_replaces_empty_line_items_array(self, adapter):
        rec_id = _seed_row(
            adapter,
            resolution_data={"line_items": []},  # empty array → backfill
            original_event={
                "order_id": "PO-EMPTY",
                "line_item": 1,
                "po_price": 5.0,
                "sap_base_price": 5.0,
                "event_type": "X",
                "line_count": 1,
            },
        )
        _run_migration(adapter)

        rd = _read_resolution_data(adapter, rec_id)
        assert len(rd["line_items"]) == 1
        assert rd["line_items"][0]["po_price"] == 5.0


# ---------------------------------------------------------------------------
# V007 trigger compatibility
# ---------------------------------------------------------------------------


class TestV007Compatibility:
    """V007 (PR #101) added a BEFORE INSERT OR UPDATE trigger that
    rejects DUPLICATE_PO rows missing required resolution_data keys.
    The backfill UPDATE must respect that contract.
    """

    _DUP_KEYS = {
        "signal_breakdown": {"po_number": 0.30, "customer_id": 0.15},
        "composite_score": 0.94,
        "classification": "AUTO_BLOCK",
        "recommended_action": "BLOCK_AND_NOTIFY",
    }

    def test_duplicate_po_with_required_keys_backfills_cleanly(self, adapter):
        rec_id = _seed_row(
            adapter,
            intent="DUPLICATE_PO",
            final_status="MANUAL_REVIEW_REQUIRED",
            resolution_data=dict(self._DUP_KEYS),  # all four keys present
            original_event={
                "order_id": "PO-DUP-LIVE",
                "line_item": 1,
                "po_price": 100.0,
                "sap_base_price": 100.0,
                "event_type": "EDI_850_DUPLICATE_PO",
                "line_count": 1,
            },
        )
        _run_migration(adapter)

        rd = _read_resolution_data(adapter, rec_id)
        assert rd["line_items"][0]["line_id"] == "PO-DUP-LIVE-1"
        # Audit-bearing keys preserved unchanged.
        for k in self._DUP_KEYS:
            assert rd[k] == self._DUP_KEYS[k]

    def test_duplicate_po_without_required_keys_is_skipped(self, adapter):
        """Pre-V007 DUPLICATE_PO row missing the four audit-bearing
        keys would fail the trigger if the migration tried to UPDATE
        it. The migration's WHERE clause excludes such rows so the
        whole backfill doesn't abort.
        """
        # Seed by temporarily disabling the V007 trigger (simulates a
        # pre-V007 row that survived V007's idempotent re-apply).
        with adapter.connection() as conn:
            conn.execute(
                "DROP TRIGGER IF EXISTS exceptions_duplicate_po_metadata_contract_insert"
            )
            conn.execute(
                "DROP TRIGGER IF EXISTS exceptions_duplicate_po_metadata_contract_update"
            )
            conn.commit()
        rec_id = _seed_row(
            adapter,
            intent="DUPLICATE_PO",
            final_status="MANUAL_REVIEW_REQUIRED",
            resolution_data={},  # no audit-bearing keys (legacy)
            original_event={
                "order_id": "PO-LEGACY-DUP",
                "line_item": 1,
                "po_price": 1.0,
                "sap_base_price": 1.0,
                "event_type": "EDI_850_DUPLICATE_PO",
                "line_count": 1,
            },
        )
        # Re-arm V007 trigger before running V008
        from db.migrations.runner import _apply_sqlite_v007
        with adapter.connection() as conn:
            _apply_sqlite_v007(conn)
        # V008 must NOT fail on the legacy row; it must skip it
        # silently and finish the migration.
        _run_migration(adapter)

        rd = _read_resolution_data(adapter, rec_id)
        # The legacy row is left as-is — line_items not added.
        assert "line_items" not in rd

    def test_duplicate_po_pre_recipe_with_null_final_status_backfills(self, adapter):
        """A DUPLICATE_PO row whose recipe hasn't run yet
        (final_status IS NULL) is exempt from V007 even if
        resolution_data is empty. The migration backfills line_items
        without tripping the trigger."""
        rec_id = _seed_row(
            adapter,
            intent="DUPLICATE_PO",
            final_status=None,  # pre-recipe → V007 doesn't fire
            resolution_data={},
            original_event={
                "order_id": "PO-DUP-PRE",
                "line_item": 1,
                "po_price": 1.0,
                "sap_base_price": 1.0,
                "event_type": "EDI_850_DUPLICATE_PO",
                "line_count": 1,
            },
        )
        _run_migration(adapter)

        rd = _read_resolution_data(adapter, rec_id)
        assert "line_items" in rd
        assert rd["line_items"][0]["line_id"] == "PO-DUP-PRE-1"


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------


def test_v008_recorded_in_schema_migrations(adapter):
    """apply_schema() runs every migration; V008 should be recorded."""
    with adapter.cursor() as cur:
        cur.execute(
            "SELECT version FROM schema_migrations WHERE version = 'V008'"
        )
        row = cur.fetchone()
    assert row is not None
