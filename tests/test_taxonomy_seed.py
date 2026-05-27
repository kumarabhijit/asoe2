"""Phase 1 — taxonomy YAML seed + DB loader tests.

Covers:
  - YAML validates against the JSON schema and structural invariants.
  - Loader writes the expected rows to a fresh SQLite DB.
  - Loader is idempotent (running twice == running once).
  - All requirement §6.2/§6.3 supergroups are present.

Reference: docs/specs/case-intent-supergroup-requirements.md §6, §7;
docs/plans/case-intent-supergroup-implementation-plan.md §3.
"""

from __future__ import annotations

import sqlite3

import pytest

from db.migrations.runner import _apply_sqlite_v017, apply_sqlite
from scripts.seed_taxonomy import load_taxonomy_sqlite, load_yaml


# ---------------------------------------------------------------------------
# Schema + invariants
# ---------------------------------------------------------------------------

def test_yaml_loads_and_validates():
    """The seed YAML parses, schema-validates, and passes invariants."""
    data = load_yaml()
    assert data["version"]
    assert len(data["supergroups"]) >= 20  # 8 API + 12 CUSTOMER per §6
    assert len(data["intents"]) >= 16
    assert len(data["labels"]) >= 36  # 1 per sg + 1 per intent at minimum


def test_required_api_supergroups_present():
    """Requirement §6.2: exactly 8 API supergroups, all named codes present."""
    data = load_yaml()
    api_sgs = {sg["code"] for sg in data["supergroups"] if sg["origin"] == "API"}
    expected = {
        "SG_BLOCK_PRICING", "SG_BLOCK_CREDIT", "SG_BLOCK_AVAILABILITY",
        "SG_BLOCK_MASTER_DATA", "SG_BLOCK_LOGISTICS", "SG_BLOCK_COMPLIANCE",
        "SG_BLOCK_ORDER_INTEGRITY", "SG_BLOCK_UNMAPPED",
    }
    assert api_sgs == expected, f"API supergroup set drifted: {api_sgs ^ expected}"


def test_required_customer_supergroups_present():
    """Requirement §6.3: 12 CUSTOMER supergroups, all named codes present."""
    data = load_yaml()
    cust_sgs = {sg["code"] for sg in data["supergroups"] if sg["origin"] == "CUSTOMER"}
    expected = {
        "SG_NEW_ORDER", "SG_ORDER_CHANGE", "SG_ORDER_STATUS_INQUIRY",
        "SG_SHIPMENT_DISCREPANCY", "SG_RETURN_RGA", "SG_LOGISTICS_CHANGE",
        "SG_BILLING_DISPUTE", "SG_DOCUMENTATION", "SG_COMPLAINT_SERVICE",
        "SG_COMPLAINT_PRODUCT", "SG_EDI_ESCALATION", "SG_NEEDS_TRIAGE",
    }
    assert cust_sgs == expected, f"CUSTOMER supergroup set drifted: {cust_sgs ^ expected}"


def test_reserved_sentinels_present():
    """SG_BLOCK_UNMAPPED, SG_NEEDS_TRIAGE, INT_UNMAPPED_PENDING_TAXONOMY,
    INT_UNKNOWN must always exist — app logic references them directly."""
    data = load_yaml()
    sg_codes = {sg["code"] for sg in data["supergroups"]}
    int_codes = {i["code"] for i in data["intents"]}
    assert "SG_BLOCK_UNMAPPED" in sg_codes
    assert "SG_NEEDS_TRIAGE" in sg_codes
    assert "INT_UNMAPPED_PENDING_TAXONOMY" in int_codes
    assert "INT_UNKNOWN" in int_codes


# ---------------------------------------------------------------------------
# SQLite loader behaviour
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_sqlite(conn)
    yield conn
    conn.close()


def test_seed_writes_all_supergroups(seeded_conn: sqlite3.Connection):
    data = load_yaml()
    cur = seeded_conn.execute("SELECT code FROM case_supergroup ORDER BY code")
    db_codes = {row[0] for row in cur.fetchall()}
    yaml_codes = {sg["code"] for sg in data["supergroups"]}
    assert db_codes == yaml_codes


def test_seed_writes_all_intents(seeded_conn: sqlite3.Connection):
    data = load_yaml()
    cur = seeded_conn.execute("SELECT code FROM case_intent ORDER BY code")
    db_codes = {row[0] for row in cur.fetchall()}
    yaml_codes = {i["code"] for i in data["intents"]}
    assert db_codes == yaml_codes


def test_seed_writes_supergroup_intent_allowed(seeded_conn: sqlite3.Connection):
    """Every intent's (supergroup_code, intent_code) is in the allowed table."""
    data = load_yaml()
    cur = seeded_conn.execute(
        "SELECT supergroup_code, intent_code FROM supergroup_intent_allowed"
    )
    db_pairs = set(cur.fetchall())
    yaml_pairs = {(i["supergroup_code"], i["code"]) for i in data["intents"]}
    assert db_pairs == yaml_pairs


def test_seed_writes_labels(seeded_conn: sqlite3.Connection):
    data = load_yaml()
    cur = seeded_conn.execute(
        "SELECT code, domain, locale FROM intent_label ORDER BY code, domain, locale"
    )
    db_rows = set(cur.fetchall())
    yaml_rows = {
        (label["code"], label["domain"], label.get("locale", "en"))
        for label in data["labels"]
    }
    assert db_rows == yaml_rows


def test_loader_is_idempotent(seeded_conn: sqlite3.Connection):
    """Re-running the loader must not duplicate rows or break invariants."""
    before_sg = seeded_conn.execute("SELECT COUNT(*) FROM case_supergroup").fetchone()[0]
    before_int = seeded_conn.execute("SELECT COUNT(*) FROM case_intent").fetchone()[0]
    before_lbl = seeded_conn.execute("SELECT COUNT(*) FROM intent_label").fetchone()[0]
    load_taxonomy_sqlite(seeded_conn)  # second pass
    after_sg = seeded_conn.execute("SELECT COUNT(*) FROM case_supergroup").fetchone()[0]
    after_int = seeded_conn.execute("SELECT COUNT(*) FROM case_intent").fetchone()[0]
    after_lbl = seeded_conn.execute("SELECT COUNT(*) FROM intent_label").fetchone()[0]
    assert (after_sg, after_int, after_lbl) == (before_sg, before_int, before_lbl)


def test_seed_recorded_in_migrations(seeded_conn: sqlite3.Connection):
    cur = seeded_conn.execute(
        "SELECT version FROM schema_migrations WHERE version = 'V017'"
    )
    assert cur.fetchone() is not None


def test_v017_idempotent_on_repeat_apply(seeded_conn: sqlite3.Connection):
    """Applying V017 a second time against an already-seeded DB is a no-op."""
    _apply_sqlite_v017(seeded_conn)  # should not raise
    cur = seeded_conn.execute("SELECT COUNT(*) FROM case_supergroup")
    assert cur.fetchone()[0] == 20
