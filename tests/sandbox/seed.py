"""Sandbox SQLite seeder — generated from the scenario catalog.

Populates a local sandbox.db with sample SAP pricing data, retailer
contracts, credit profiles, and EDI 850 events that exercise every
supported intent (CONTRACTUAL_CORRECTION, CREDIT_BLOCK,
MASS_PRICING_ERROR, DUPLICATE_PO, PRICE_HOLD_RELEASE, EDI_MISMATCH,
BACK_ORDER, OVER_MAX, MIN_ORDER_QTY, PALLET_CONFIG, DELIVERY_DELAY).

Source of truth
---------------
The 8 domain tables are built from ``fixtures/scenarios/catalog.yaml``
(RFC: ``asoe-ui/docs/synthetic-data-placement-rfc.md``, Decision A).
This module no longer carries inline data — the catalog is the single
declarative source, so the asoe2 sandbox seed and the asoe-ui mock
layer are generated from the same file. The coverage lock
``tests/sandbox/test_catalog_coverage.py`` asserts ``seed >= catalog``.

Usage
-----
    python tests/sandbox/seed.py                      # -> tests/sandbox/sandbox.db
    python tests/sandbox/seed.py --db /tmp/test.db    # custom path
    python tests/sandbox/seed.py --reset              # drop and re-create

The resulting .db file is gitignored.  Only this script + the catalog
are committed.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import yaml

DB_DEFAULT = Path(__file__).parent / "sandbox.db"

# fixtures/scenarios/catalog.yaml lives at the repo root; seed.py is at
# tests/sandbox/seed.py, so parents[2] is the repo root.
CATALOG_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "scenarios" / "catalog.yaml"

# ---------------------------------------------------------------------------
# Schema (inline so the script is self-contained)
# ---------------------------------------------------------------------------

SCHEMA = """
-- Customer master data — retailers placing orders via EDI 850
CREATE TABLE IF NOT EXISTS customers (
    retailer_id   TEXT PRIMARY KEY,
    name          TEXT    NOT NULL,
    region        TEXT    NOT NULL DEFAULT 'US-EAST',
    tier          TEXT    NOT NULL DEFAULT 'STANDARD',
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL
);

-- Distribution centers — fulfillment locations
CREATE TABLE IF NOT EXISTS distribution_centers (
    dc_id         TEXT PRIMARY KEY,
    name          TEXT    NOT NULL,
    region        TEXT    NOT NULL,
    capacity_pct  REAL    NOT NULL DEFAULT 100.0,
    active        INTEGER NOT NULL DEFAULT 1
);

-- SAP condition-based pricing — authoritative base prices
CREATE TABLE IF NOT EXISTS sap_pricing (
    sku          TEXT PRIMARY KEY,
    description  TEXT,
    category     TEXT    NOT NULL DEFAULT 'GENERAL',
    base_price   REAL    NOT NULL,
    currency     TEXT    NOT NULL DEFAULT 'USD',
    dc_id        TEXT,
    updated_at   TEXT    NOT NULL
);

-- Retailer-specific contract prices negotiated against the SAP base price.
-- discount_pct is the pre-computed percentage discount from base_price.
-- PriceAdjustmentRecipe rejects discounts > 15 %.
CREATE TABLE IF NOT EXISTS retailer_contracts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    retailer_id     TEXT    NOT NULL,
    sku             TEXT    NOT NULL,
    contract_price  REAL    NOT NULL,
    discount_pct    REAL    NOT NULL,
    contract_start  TEXT    NOT NULL,
    contract_end    TEXT    NOT NULL,
    UNIQUE(retailer_id, sku)
);

-- Active promotions that may cause legitimate price mismatches
CREATE TABLE IF NOT EXISTS promotions (
    promo_id      TEXT PRIMARY KEY,
    sku           TEXT    NOT NULL,
    promo_type    TEXT    NOT NULL DEFAULT 'SEASONAL',
    discount_pct  REAL    NOT NULL,
    start_date    TEXT    NOT NULL,
    end_date      TEXT    NOT NULL,
    region        TEXT,
    active        INTEGER NOT NULL DEFAULT 1
);

-- Credit exposure data used by CreditHoldReleaseRecipe.
-- current_exposure > credit_limit triggers a credit block event.
CREATE TABLE IF NOT EXISTS credit_profiles (
    retailer_id       TEXT PRIMARY KEY,
    credit_limit      REAL NOT NULL,
    current_exposure  REAL NOT NULL,
    risk_rating       TEXT NOT NULL DEFAULT 'NORMAL',
    last_review_date  TEXT,
    updated_at        TEXT NOT NULL
);

-- Pre-loaded EDI 850 events covering all supported intents.
-- metadata is a JSON blob carrying intent-specific fields
-- (signal_scores for DUPLICATE_PO, credit fields for CREDIT_BLOCK, etc.)
CREATE TABLE IF NOT EXISTS edi_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT    UNIQUE NOT NULL,
    event_type  TEXT    NOT NULL,
    order_id    TEXT    NOT NULL,
    retailer_id TEXT,
    sku         TEXT,
    po_price    REAL,
    sap_price   REAL,
    line_count  INTEGER NOT NULL DEFAULT 1,
    dc_id       TEXT,
    metadata    TEXT    NOT NULL DEFAULT '{}',
    created_at  TEXT    NOT NULL
);
"""

_NOW = datetime.now(timezone.utc).isoformat()

# ---------------------------------------------------------------------------
# Catalog loading
# ---------------------------------------------------------------------------


def load_catalog(path: Path | None = None) -> Dict[str, Any]:
    """Parse and return the scenario catalog.

    The catalog is the single declarative source the seed is built from.
    Kept as a plain dict (not a typed model) so this seeder has no import
    dependency on the application contracts — it runs with only ``pyyaml``
    + stdlib, which keeps the sandbox bootstrap light.
    """
    with open(path or CATALOG_PATH, "r", encoding="utf-8") as fh:
        catalog = yaml.safe_load(fh)
    if not isinstance(catalog, dict) or "entities" not in catalog:
        raise ValueError(f"Malformed scenario catalog at {path or CATALOG_PATH}")
    return catalog


# Columns the edi_events table consumes from a scenario entry. Projection
# fields (intent / lifecycle / shadow_verdict / revenue_at_risk / diagnosis
# / origin) are catalog metadata for the UI generator and are NOT persisted
# into the sandbox edi_events row.
_EVENT_COLUMNS = (
    "event_type", "order_id", "retailer_id", "sku",
    "po_price", "sap_price", "line_count", "dc_id",
)


def _scenario_to_event(scn: Dict[str, Any]) -> Dict[str, Any]:
    """Project a catalog scenario onto an edi_events row dict."""
    row = {col: scn.get(col) for col in _EVENT_COLUMNS}
    row["event_id"] = scn["id"]
    row["line_count"] = scn.get("line_count", 1)
    row["metadata"] = json.dumps(scn.get("metadata", {}))
    return row


# ---------------------------------------------------------------------------
# Seeder
# ---------------------------------------------------------------------------

def seed(db_path: Path, reset: bool = False, catalog_path: Path | None = None) -> None:
    """Create tables and insert catalog-sourced data into *db_path*."""
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if reset and db_path.exists():
        db_path.unlink()
        print(f"Removed existing database: {db_path}")

    catalog = load_catalog(catalog_path)
    ent = catalog["entities"]
    consts = catalog.get("contract_constants", {})
    contract_start = consts.get("contract_start", "2025-01-01")
    contract_end = consts.get("contract_end", "2026-12-31")

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    # Customers
    conn.executemany(
        "INSERT OR REPLACE INTO customers "
        "  (retailer_id, name, region, tier, active, created_at) "
        "VALUES (:retailer_id, :name, :region, :tier, :active, :created_at)",
        [{**c, "active": int(c.get("active", 1)), "created_at": _NOW}
         for c in ent["customers"]],
    )

    # Distribution Centers
    conn.executemany(
        "INSERT OR REPLACE INTO distribution_centers "
        "  (dc_id, name, region, capacity_pct, active) "
        "VALUES (:dc_id, :name, :region, :capacity_pct, :active)",
        [{**d, "active": int(d.get("active", 1))} for d in ent["distribution_centers"]],
    )

    # SAP pricing
    conn.executemany(
        "INSERT OR REPLACE INTO sap_pricing "
        "  (sku, description, category, base_price, currency, dc_id, updated_at) "
        "VALUES (:sku, :description, :category, :base_price, :currency, :dc_id, :updated_at)",
        [{**p, "updated_at": _NOW} for p in ent["sap_pricing"]],
    )

    # Retailer contracts
    conn.executemany(
        "INSERT OR REPLACE INTO retailer_contracts "
        "  (retailer_id, sku, contract_price, discount_pct, contract_start, contract_end) "
        "VALUES (:retailer_id, :sku, :contract_price, :discount_pct, :contract_start, :contract_end)",
        [{**c, "contract_start": contract_start, "contract_end": contract_end}
         for c in ent["retailer_contracts"]],
    )

    # Promotions
    conn.executemany(
        "INSERT OR REPLACE INTO promotions "
        "  (promo_id, sku, promo_type, discount_pct, start_date, end_date, region, active) "
        "VALUES (:promo_id, :sku, :promo_type, :discount_pct, :start_date, :end_date, :region, :active)",
        [{**p, "region": p.get("region"), "active": int(p.get("active", 1))}
         for p in ent["promotions"]],
    )

    # Credit profiles
    conn.executemany(
        "INSERT OR REPLACE INTO credit_profiles "
        "  (retailer_id, credit_limit, current_exposure, risk_rating, last_review_date, updated_at) "
        "VALUES (:retailer_id, :credit_limit, :current_exposure, :risk_rating, :last_review_date, :updated_at)",
        [{**c, "updated_at": _NOW} for c in ent["credit_profiles"]],
    )

    # EDI events (projected from scenarios)
    events = [_scenario_to_event(s) for s in catalog.get("scenarios", [])]
    for ev in events:
        conn.execute(
            "INSERT OR REPLACE INTO edi_events "
            "  (event_id, event_type, order_id, retailer_id, sku, "
            "   po_price, sap_price, line_count, dc_id, metadata, created_at) "
            "VALUES "
            "  (:event_id, :event_type, :order_id, :retailer_id, :sku, "
            "   :po_price, :sap_price, :line_count, :dc_id, :metadata, :created_at)",
            {**ev, "created_at": _NOW},
        )

    conn.commit()
    conn.close()

    print(f"Seeded: {db_path}")
    print(f"    {len(ent['customers'])} customers")
    print(f"    {len(ent['distribution_centers'])} distribution centers")
    print(f"    {len(ent['sap_pricing'])} SKUs")
    print(f"    {len(ent['retailer_contracts'])} retailer contracts")
    print(f"    {len(ent['promotions'])} promotions")
    print(f"    {len(ent['credit_profiles'])} credit profiles")
    print(f"    {len(events)} EDI events")
    print(f"    {len(catalog.get('email_scenarios', []))} email scenarios (fixtures)")
    print()
    _print_event_summary(catalog)


def _print_event_summary(catalog: Dict[str, Any]) -> None:
    print("EDI event summary:")
    for scn in catalog.get("scenarios", []):
        print(f"  {scn['id']:18s}  {scn.get('intent', 'UNKNOWN'):24s}  {scn['order_id']}")


# ---------------------------------------------------------------------------
# Read helpers (UI / CLI consumers)
# ---------------------------------------------------------------------------

def load_events(db_path: Path) -> List[dict]:
    """Return all EDI events from the database as dicts (for the UI)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM edi_events ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def load_customers(db_path: Path) -> List[dict]:
    """Return all active customers from the database as dicts."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM customers WHERE active = 1 ORDER BY retailer_id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def load_promotions(db_path: Path) -> List[dict]:
    """Return all active promotions from the database as dicts."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM promotions WHERE active = 1 ORDER BY promo_id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def lookup_sap_price(db_path: Path, sku: str) -> float | None:
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT base_price FROM sap_pricing WHERE sku = ?", (sku,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def lookup_customer(db_path: Path, retailer_id: str) -> dict | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM customers WHERE retailer_id = ?", (retailer_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def lookup_credit_profile(db_path: Path, retailer_id: str) -> dict | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM credit_profiles WHERE retailer_id = ?", (retailer_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the ASOE sandbox SQLite database.")
    parser.add_argument(
        "--db", default=str(DB_DEFAULT),
        help=f"Path to the .db file (default: {DB_DEFAULT})",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Delete an existing .db file before seeding",
    )
    args = parser.parse_args()
    seed(Path(args.db), reset=args.reset)
