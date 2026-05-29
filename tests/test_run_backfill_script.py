"""ADR-038 Phase H.7 — `scripts/run_backfill.py` CLI tests.

The script wraps `agents.backfill` with arg parsing and a JSON
report. The two backfill passes themselves are covered by
`tests/test_compaction_sla_backfill.py`; these tests cover the
CLI surface — arg combinations, exit codes, tier-map loading,
and dry-run propagation.
"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from uuid import uuid4

import pytest

from agents.backfill import BackfillReport
from api.store import case_store, exception_store, ChildCase
from contracts.models import OrderEvent
from scripts.run_backfill import main as run_backfill_main


@pytest.fixture(autouse=True)
def _clear_stores():
    case_store.clear()
    exception_store.clear()
    yield
    case_store.clear()
    exception_store.clear()


def _make_orphan_record(*, order_id: str, tenant_id: str = "tenant-a") -> ChildCase:
    """Insert an orphaned record (parent_case_id=None) with a valid
    persisted ``original_event`` so backfill has something to act on."""
    event = OrderEvent(
        order_id=order_id,
        po_price=100.0,
        sap_base_price=110.0,
        event_type="EDI_850_PRICE_MISMATCH",
        retailer_id="walmart",
    )
    record = ChildCase(
        tenant_id=tenant_id,
        order_id=order_id,
        event_type="EDI_850_PRICE_MISMATCH",
        trace_id=str(uuid4()),
        original_event=event.model_dump(),
    )
    exception_store._records[record.id] = record
    return record


def _run(argv: list[str]) -> tuple[int, str, str]:
    # Backfill is now tenant-scoped (--tenant required). Every fixture
    # here seeds tenant-a, so inject it unless a test sets its own.
    if "--tenant" not in argv:
        argv = ["--tenant", "tenant-a", *argv]
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = run_backfill_main(argv)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Pass 1
# ---------------------------------------------------------------------------

class TestPass1:
    def test_default_runs_pass_1(self):
        _make_orphan_record(order_id="PO-1")
        _make_orphan_record(order_id="PO-2")
        rc, stdout, _ = _run([])
        assert rc == 0
        body = json.loads(stdout)
        assert len(body["reports"]) == 1
        report = body["reports"][0]
        assert report["pass"] == "1"
        assert report["records_scanned"] == 2
        assert report["cases_opened"] == 2
        assert report["records_skipped_no_event"] == 0

    def test_pass_1_idempotent_second_run_no_op(self):
        _make_orphan_record(order_id="PO-1")
        rc1, _, _ = _run(["--pass", "1"])
        assert rc1 == 0
        # Pass 1 again — every record now has parent_case_id, so
        # cases_opened == 0.
        rc2, stdout, _ = _run(["--pass", "1"])
        assert rc2 == 0
        report = json.loads(stdout)["reports"][0]
        # Idempotent: the link is persisted, so the second run finds no
        # orphans to scan at all (list_orphans returns only NULL-parent
        # rows) — strictly better than the old scan-all-then-skip.
        assert report["records_scanned"] == 0
        assert report["cases_opened"] == 0

    def test_skipped_when_original_event_missing(self):
        record = ChildCase(
            tenant_id="tenant-a",
            order_id="PO-NO-EVT",
            event_type="EDI_850_PRICE_MISMATCH",
            trace_id="trace-x",
        )
        exception_store._records[record.id] = record
        rc, stdout, _ = _run([])
        assert rc == 0
        report = json.loads(stdout)["reports"][0]
        assert report["records_skipped_no_event"] == 1
        assert report["cases_opened"] == 0


# ---------------------------------------------------------------------------
# Pass 2 / both
# ---------------------------------------------------------------------------

class TestPass2:
    def test_pass_2_only(self):
        rc, stdout, _ = _run(["--pass", "2"])
        assert rc == 0
        body = json.loads(stdout)
        assert len(body["reports"]) == 1
        assert body["reports"][0]["pass"] == "2"

    def test_both_passes(self):
        _make_orphan_record(order_id="PO-1")
        rc, stdout, _ = _run(["--pass", "both"])
        assert rc == 0
        labels = [r["pass"] for r in json.loads(stdout)["reports"]]
        assert labels == ["1", "2"]

    def test_dry_run_does_not_mutate(self):
        _make_orphan_record(order_id="PO-1")
        # Pass 1 first to materialise cases.
        _run(["--pass", "1"])
        before = len(case_store._cases)
        # Pass 2 dry-run.
        rc, _, _ = _run(["--pass", "2", "--dry-run"])
        assert rc == 0
        assert len(case_store._cases) == before


# ---------------------------------------------------------------------------
# Tier-map handling + arg validation
# ---------------------------------------------------------------------------

class TestArgValidation:
    def test_tier_map_path_missing_returns_2(self, tmp_path):
        rc, _, stderr = _run(
            ["--pass", "1", "--tier-map", str(tmp_path / "nope.json")],
        )
        assert rc == 2
        assert "does not exist" in stderr

    def test_tier_map_invalid_json_returns_2(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        rc, _, stderr = _run(["--tier-map", str(path)])
        assert rc == 2
        assert "must be an object" in stderr

    def test_valid_tier_map_loaded(self, tmp_path):
        _make_orphan_record(order_id="PO-T")
        path = tmp_path / "tiers.json"
        path.write_text(
            json.dumps({"tenant-a": "Strategic"}), encoding="utf-8",
        )
        rc, stdout, _ = _run(["--tier-map", str(path)])
        assert rc == 0
        # Strategic tier sets a 4h SLA — confirm a deadline got stamped.
        case_id = next(iter(json.loads(stdout)["reports"][0]["record_to_case"].values()))
        assert case_store.get(case_id).sla_deadline is not None
