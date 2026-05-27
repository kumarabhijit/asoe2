"""Phase 6 — SAP block-code reconciliation tests.

Authority: docs/specs/case-intent-supergroup-requirements.md §3.9 + §8.8.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from db.migrations.runner import apply_sqlite
from scripts.reconcile_sap_block_codes import (
    ReconciliationReport,
    SapBlockCode,
    format_report,
    load_sap_snapshot,
    main,
    reconcile,
)


# ---------------------------------------------------------------------------
# Pure-function tests (no DB / FS)
# ---------------------------------------------------------------------------

def test_matched_codes_produce_empty_drift():
    sap = [SapBlockCode("01", "LIFSK"), SapBlockCode("02", "LIFSP")]
    db = [
        ("INT_A", "01", "LIFSK"),
        ("INT_B", "02", "LIFSP"),
    ]
    report = reconcile(sap, db)
    assert report.matched_count == 2
    assert report.new_in_sap == []
    assert report.stale_in_db == []
    assert report.malformed_in_db == []
    assert report.has_drift is False


def test_new_code_in_sap_surfaces():
    sap = [
        SapBlockCode("01", "LIFSK", "manual hold"),
        SapBlockCode("ZP", "LIFSK", "new pricing-pending Z-code"),
    ]
    db = [("INT_A", "01", "LIFSK")]
    report = reconcile(sap, db)
    assert report.matched_count == 1
    assert len(report.new_in_sap) == 1
    assert report.new_in_sap[0].sap_block_code == "ZP"
    assert report.has_drift is True


def test_stale_code_in_db_surfaces():
    sap = [SapBlockCode("01", "LIFSK")]
    db = [
        ("INT_A", "01", "LIFSK"),
        ("INT_GHOST", "99", "FAKSK"),  # no longer in SAP
    ]
    report = reconcile(sap, db)
    assert report.matched_count == 1
    assert len(report.stale_in_db) == 1
    assert report.stale_in_db[0][0] == "INT_GHOST"


def test_malformed_db_row_surfaces_separately():
    """A case_intent row with sap_block_code set but sap_block_field
    NULL is unreconcilable; surface it as malformed_in_db so the
    Steward can fix the seed (review finding #4)."""
    sap = [SapBlockCode("01", "LIFSK")]
    db = [
        ("INT_A", "01", "LIFSK"),
        ("INT_HALF_BAKED", "Z9", None),  # missing field
    ]
    report = reconcile(sap, db)
    assert report.matched_count == 1
    assert len(report.malformed_in_db) == 1
    assert report.malformed_in_db[0] == ("INT_HALF_BAKED", "Z9")
    assert report.has_drift is True
    # The malformed row does NOT also show up as stale.
    assert all(s[0] != "INT_HALF_BAKED" for s in report.stale_in_db)


def test_db_rows_with_null_sap_block_code_ignored():
    """CUSTOMER intents / sentinels have no sap_block_code — they're
    out of scope for SAP reconciliation."""
    sap: list[SapBlockCode] = []
    db = [
        ("INT_CUSTOMER", None, None),
        ("INT_SENTINEL", None, None),
    ]
    report = reconcile(sap, db)
    assert report.matched_count == 0
    assert report.malformed_in_db == []
    assert report.stale_in_db == []
    assert report.has_drift is False


def test_same_code_different_field_treated_as_distinct():
    """A two-char SAP code is reused across tables — '01' on LIFSK is
    not the same block as '01' on FAKSK. Reconciliation keys on the
    pair, not the code alone."""
    sap = [
        SapBlockCode("01", "LIFSK"),
        SapBlockCode("01", "FAKSK"),
    ]
    db = [("INT_A", "01", "LIFSK")]  # only one of the two mapped
    report = reconcile(sap, db)
    assert report.matched_count == 1
    assert len(report.new_in_sap) == 1
    assert report.new_in_sap[0].sap_block_field == "FAKSK"


def test_sorted_output_is_deterministic():
    """Two runs over the same inputs produce identical reports."""
    sap = [
        SapBlockCode("ZB", "LIFSP"),
        SapBlockCode("01", "LIFSK"),
        SapBlockCode("ZA", "ABGRU"),
    ]
    db: list[tuple[str, str, str]] = []
    r1 = reconcile(sap, db)
    r2 = reconcile(sap, db)
    assert [c.key for c in r1.new_in_sap] == [c.key for c in r2.new_in_sap]


# ---------------------------------------------------------------------------
# Snapshot loader
# ---------------------------------------------------------------------------

def test_load_sap_snapshot_parses_csv(tmp_path: Path):
    snapshot = tmp_path / "sap.csv"
    snapshot.write_text(
        "sap_block_code,sap_block_field,description\n"
        "01,LIFSK,Manual hold\n"
        "ZP,LIFSK,Pricing pending Z-code\n",
        encoding="utf-8",
    )
    codes = load_sap_snapshot(snapshot)
    assert len(codes) == 2
    assert codes[0].sap_block_code == "01"
    assert codes[1].description == "Pricing pending Z-code"


def test_load_sap_snapshot_rejects_missing_columns(tmp_path: Path):
    snapshot = tmp_path / "bad.csv"
    snapshot.write_text("sap_block_code\n01\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required columns"):
        load_sap_snapshot(snapshot)


def test_load_sap_snapshot_rejects_invalid_sap_block_field(tmp_path: Path):
    """A snapshot row whose sap_block_field is not in the allowed set
    (typo, e.g. ``LIFSL`` for ``LIFSK``) must raise. Otherwise the
    drift report would falsely surface the typo as a 'new' code AND
    mark the valid mapping as 'stale' (review finding #5)."""
    snapshot = tmp_path / "typo.csv"
    snapshot.write_text(
        "sap_block_code,sap_block_field,description\n"
        "01,LIFSL,Typo — meant LIFSK\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="LIFSL"):
        load_sap_snapshot(snapshot)


def test_load_sap_snapshot_rejects_empty_block_code(tmp_path: Path):
    snapshot = tmp_path / "blank.csv"
    snapshot.write_text(
        "sap_block_code,sap_block_field,description\n"
        ",LIFSK,empty code\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="empty sap_block_code"):
        load_sap_snapshot(snapshot)


# ---------------------------------------------------------------------------
# DB integration — actual SQLite migration chain seeded
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "asoe.db"
    conn = sqlite3.connect(db_path)
    apply_sqlite(conn)
    conn.close()
    return db_path


def test_main_returns_zero_when_no_drift(tmp_path: Path, seeded_db: Path,
                                          capsys: pytest.CaptureFixture):
    """No SAP codes mapped in the seed today; an empty snapshot
    produces zero drift."""
    snapshot = tmp_path / "empty.csv"
    snapshot.write_text("sap_block_code,sap_block_field,description\n",
                         encoding="utf-8")
    rc = main([
        "--sap-snapshot", str(snapshot),
        "--database-url", f"sqlite:///{seeded_db}",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "matched: 0" in captured.out


def test_main_returns_two_with_drift_flag(tmp_path: Path, seeded_db: Path):
    """A new code in the SAP snapshot causes the --exit-nonzero-on-drift
    flag to return 2 (CI-friendly)."""
    snapshot = tmp_path / "drift.csv"
    snapshot.write_text(
        "sap_block_code,sap_block_field,description\n"
        "ZQ,LIFSK,Surprise Z-code\n",
        encoding="utf-8",
    )
    rc = main([
        "--sap-snapshot", str(snapshot),
        "--database-url", f"sqlite:///{seeded_db}",
        "--exit-nonzero-on-drift",
    ])
    assert rc == 2


def test_main_without_drift_flag_returns_zero_even_on_drift(
    tmp_path: Path, seeded_db: Path,
):
    """Without ``--exit-nonzero-on-drift`` the script always returns 0
    when reconciliation succeeded. The flag is opt-in for the CI cron."""
    snapshot = tmp_path / "drift.csv"
    snapshot.write_text(
        "sap_block_code,sap_block_field,description\n"
        "ZQ,LIFSK,Surprise Z-code\n",
        encoding="utf-8",
    )
    rc = main([
        "--sap-snapshot", str(snapshot),
        "--database-url", f"sqlite:///{seeded_db}",
    ])
    assert rc == 0


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def test_format_report_includes_new_stale_and_malformed_sections():
    report = ReconciliationReport(
        new_in_sap=[SapBlockCode("ZP", "LIFSK", "Pricing pending")],
        stale_in_db=[("INT_GHOST", "99", "FAKSK")],
        malformed_in_db=[("INT_HALF_BAKED", "Z9")],
        matched_count=3,
    )
    text = format_report(report)
    assert "matched: 3" in text
    assert "ZP" in text
    assert "Pricing pending" in text
    assert "INT_GHOST" in text
    assert "INT_HALF_BAKED" in text
    assert "malformed" in text.lower()
