"""Coverage lock: the sandbox seed is a faithful superset of the catalog.

RFC: asoe-ui/docs/synthetic-data-placement-rfc.md (Decision A, Phase 3).

`fixtures/scenarios/catalog.yaml` is the single declarative source the
sandbox seed is generated from. These tests assert two invariants:

  1. **seed >= catalog** — every entity row and every scenario declared
     in the catalog materialises into the seeded SQLite. A scenario that
     is authored but never seeded is a silent coverage hole.
  2. **Enum governance (Guardrail #2 / #4)** — every catalog
     `intent` / `lifecycle` / `shadow_verdict` is a member of the
     authoritative `contracts.models` enums (the same vocabulary
     `/health` serves). The catalog must never become a second,
     drifting enum authority.

Remove either property (drop a seeded row, or add an off-taxonomy enum
value) and the matching test fails — that is the lock.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from contracts.models import Intent, LifecycleState, ShadowStatus
from tests.sandbox.seed import CATALOG_PATH, load_catalog, seed


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory):
    db = tmp_path_factory.mktemp("catalog_coverage") / "sandbox.db"
    seed(db, reset=True)
    return db


def _column(db: Path, table: str, column: str) -> set:
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute(f"SELECT {column} FROM {table}")}
    finally:
        conn.close()


def test_catalog_file_exists():
    assert CATALOG_PATH.exists(), f"scenario catalog missing at {CATALOG_PATH}"


# ---------------------------------------------------------------------------
# seed >= catalog (entities)
# ---------------------------------------------------------------------------

ENTITY_KEY = {
    "customers": ("customers", "retailer_id", "retailer_id"),
    "distribution_centers": ("distribution_centers", "dc_id", "dc_id"),
    "sap_pricing": ("sap_pricing", "sku", "sku"),
    "promotions": ("promotions", "promo_id", "promo_id"),
    "credit_profiles": ("credit_profiles", "retailer_id", "retailer_id"),
}


@pytest.mark.parametrize("entity", sorted(ENTITY_KEY))
def test_seed_contains_every_catalog_entity(entity, catalog, seeded_db):
    table, table_col, cat_key = ENTITY_KEY[entity]
    seeded = _column(seeded_db, table, table_col)
    declared = {row[cat_key] for row in catalog["entities"][entity]}
    missing = declared - seeded
    assert not missing, f"{entity} declared in catalog but absent from seed: {sorted(missing)}"


def test_seed_contains_every_catalog_contract(catalog, seeded_db):
    conn = sqlite3.connect(seeded_db)
    try:
        seeded = {(r[0], r[1]) for r in conn.execute(
            "SELECT retailer_id, sku FROM retailer_contracts")}
    finally:
        conn.close()
    declared = {(c["retailer_id"], c["sku"]) for c in catalog["entities"]["retailer_contracts"]}
    missing = declared - seeded
    assert not missing, f"retailer_contracts declared in catalog but absent from seed: {sorted(missing)}"


# ---------------------------------------------------------------------------
# seed >= catalog (scenarios -> edi_events)
# ---------------------------------------------------------------------------


def test_seed_contains_every_catalog_scenario(catalog, seeded_db):
    seeded = _column(seeded_db, "edi_events", "event_id")
    declared = {s["id"] for s in catalog["scenarios"]}
    missing = declared - seeded
    assert not missing, f"scenarios declared in catalog but absent from seed: {sorted(missing)}"


def test_scenario_ids_are_unique(catalog):
    ids = [s["id"] for s in catalog["scenarios"]]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate scenario ids in catalog: {sorted(dupes)}"


def test_scenarios_reference_known_entities(catalog):
    rids = {c["retailer_id"] for c in catalog["entities"]["customers"]}
    dcs = {d["dc_id"] for d in catalog["entities"]["distribution_centers"]}
    skus = {p["sku"] for p in catalog["entities"]["sap_pricing"]}
    problems = []
    for s in catalog["scenarios"]:
        if s.get("retailer_id") and s["retailer_id"] not in rids:
            problems.append((s["id"], "retailer_id", s["retailer_id"]))
        if s.get("dc_id") and s["dc_id"] not in dcs:
            problems.append((s["id"], "dc_id", s["dc_id"]))
        # New (appendix) scenarios must bind a priced SKU. Baseline events
        # intentionally carry event-only phantom SKUs (SKU-1xx..7xx) that
        # predate the priced master and are exempt from this check.
        if s.get("origin") == "appendix" and s.get("sku") and s["sku"] not in skus:
            problems.append((s["id"], "sku", s["sku"]))
    assert not problems, f"scenarios reference unknown entities: {problems}"


# ---------------------------------------------------------------------------
# Enum governance (Guardrail #2 / #4) — catalog binds to the real taxonomy
# ---------------------------------------------------------------------------

_INTENTS = {e.value for e in Intent}
_LIFECYCLES = {e.value for e in LifecycleState}
_VERDICTS = {e.value for e in ShadowStatus}


def _all_entries(catalog):
    return list(catalog["scenarios"]) + list(catalog.get("email_scenarios", []))


def test_catalog_intents_are_governed(catalog):
    bad = [(s["id"], s["intent"]) for s in _all_entries(catalog)
           if s.get("intent") and s["intent"] not in _INTENTS]
    assert not bad, f"catalog intents off the contracts.models.Intent taxonomy: {bad}"


def test_catalog_lifecycles_are_governed(catalog):
    bad = [(s["id"], s["lifecycle"]) for s in _all_entries(catalog)
           if s.get("lifecycle") and s["lifecycle"] not in _LIFECYCLES]
    assert not bad, f"catalog lifecycles off the contracts.models.LifecycleState taxonomy: {bad}"


def test_catalog_verdicts_are_governed(catalog):
    bad = [(s["id"], s["shadow_verdict"]) for s in _all_entries(catalog)
           if s.get("shadow_verdict") and s["shadow_verdict"] not in _VERDICTS]
    assert not bad, f"catalog shadow_verdicts off the contracts.models.ShadowStatus taxonomy: {bad}"
