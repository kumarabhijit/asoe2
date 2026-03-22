"""Sandbox SQLite seeder.

Populates a local sandbox.db with sample SAP pricing data, retailer contracts,
credit profiles, and EDI 850 events that exercise all four supported intents:
  CONTRACTUAL_CORRECTION, CREDIT_BLOCK, MASS_PRICING_ERROR, DUPLICATE_PO

Usage
-----
    python tests/sandbox/seed.py                      # → tests/sandbox/sandbox.db
    python tests/sandbox/seed.py --db /tmp/test.db    # custom path
    python tests/sandbox/seed.py --reset              # drop and re-create

The resulting .db file is gitignored.  Only this script is committed.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_DEFAULT = Path(__file__).parent / "sandbox.db"

# ---------------------------------------------------------------------------
# Schema (inline so the script is self-contained)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS sap_pricing (
    sku          TEXT PRIMARY KEY,
    description  TEXT,
    base_price   REAL    NOT NULL,
    currency     TEXT    NOT NULL DEFAULT 'USD',
    updated_at   TEXT    NOT NULL
);

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

CREATE TABLE IF NOT EXISTS credit_profiles (
    retailer_id       TEXT PRIMARY KEY,
    credit_limit      REAL NOT NULL,
    current_exposure  REAL NOT NULL,
    updated_at        TEXT NOT NULL
);

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
    metadata    TEXT    NOT NULL DEFAULT '{}',
    created_at  TEXT    NOT NULL
);
"""

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

_SAP_PRICING = [
    ("SKU-001", "Premium Detergent 5L",   100.00, "USD"),
    ("SKU-002", "Budget Detergent 2L",     45.00, "USD"),
    ("SKU-003", "Fabric Softener 3L",      62.00, "USD"),
    ("SKU-004", "Dish Soap 1L",            18.00, "USD"),
    ("SKU-005", "All-Purpose Cleaner 4L",  55.00, "USD"),
]

_RETAILER_CONTRACTS = [
    # retailer_id, sku, contract_price, discount_pct
    ("R-01", "SKU-001",  90.00, 10.0),  # 10 % — within 15 % threshold
    ("R-01", "SKU-002",  40.50, 10.0),
    ("R-02", "SKU-001",  92.00,  8.0),  # 8 % — within threshold
    ("R-02", "SKU-003",  55.80, 10.0),
    ("R-03", "SKU-001",  78.00, 22.0),  # 22 % — EXCEEDS threshold → recipe FAILED
    ("R-04", "SKU-004",  16.00, 11.1),
    ("R-05", "SKU-005",  52.25,  4.9),
]

_CREDIT_PROFILES = [
    # retailer_id, credit_limit, current_exposure
    ("R-01", 10_000.00, 10_100.00),  # over limit by $100 — within $5k tolerance, released
    ("R-02", 15_000.00, 14_600.00),  # gap $400  — released
    ("R-03",  5_000.00,  4_800.00),  # gap $200  — released
    ("R-05", 20_000.00, 25_500.00),  # OVER LIMIT by $5,500 → REJECTED
]

_NOW = datetime.now(timezone.utc).isoformat()
_CONTRACT_START = "2025-01-01"
_CONTRACT_END   = "2026-12-31"

_EDI_EVENTS = [
    # ── CONTRACTUAL_CORRECTION — price within 15 % discount ────────────────
    {
        "event_id":    "EVT-CC-001",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-1001",
        "retailer_id": "R-01",
        "sku":         "SKU-001",
        "po_price":    90.0,
        "sap_price":   100.0,
        "line_count":  1,
        "metadata":    json.dumps({"contract_ref": "CTR-R01-2024", "note": "10% contract discount"}),
    },
    {
        "event_id":    "EVT-CC-002",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-1002",
        "retailer_id": "R-02",
        "sku":         "SKU-001",
        "po_price":    92.0,
        "sap_price":   100.0,
        "line_count":  1,
        "metadata":    json.dumps({"contract_ref": "CTR-R02-2024", "note": "8% contract discount"}),
    },
    # ── CONTRACTUAL_CORRECTION — discount EXCEEDS 15 %, recipe returns FAILED
    {
        "event_id":    "EVT-CC-003",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-1003",
        "retailer_id": "R-03",
        "sku":         "SKU-001",
        "po_price":    78.0,
        "sap_price":   100.0,
        "line_count":  1,
        "metadata":    json.dumps({"contract_ref": "CTR-R03-2024", "discount_claimed": "22%"}),
    },
    # ── CREDIT_BLOCK — shadow YELLOW → MANUAL_REVIEW_REQUIRED ──────────────
    {
        "event_id":    "EVT-CB-001",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-2001",
        "retailer_id": "R-01",
        "sku":         "SKU-002",
        "po_price":    100.0,
        "sap_price":   100.0,
        "line_count":  1,
        "metadata":    json.dumps({
            "requester_role":    "ORDER_MANAGER",
            "credit_limit":      10000.0,
            "current_exposure":  10100.0,
        }),
    },
    {
        "event_id":    "EVT-CB-002",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-2002",
        "retailer_id": "R-05",
        "sku":         "SKU-005",
        "po_price":    100.0,
        "sap_price":   100.0,
        "line_count":  1,
        "metadata":    json.dumps({
            "requester_role":    "FINANCE_DIRECTOR",
            "credit_limit":      20000.0,
            "current_exposure":  25500.0,
            "note":              "exposure $5,500 over limit — rejected",
        }),
    },
    # ── MASS_PRICING_ERROR — line_count > 10, shadow RED → BLOCKED ─────────
    {
        "event_id":    "EVT-MPE-001",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-3001",
        "retailer_id": "R-02",
        "sku":         "SKU-003",
        "po_price":    50.0,
        "sap_price":   62.0,
        "line_count":  15,
        "metadata":    json.dumps({"batch_ref": "BATCH-2024-01", "note": "15-line batch — systemic risk"}),
    },
    # ── DUPLICATE_PO — composite score 0.98 → AUTO_BLOCK ───────────────────
    {
        "event_id":    "EVT-DPO-001",
        "event_type":  "EDI_850_DUPLICATE_PO",
        "order_id":    "PO-9001",
        "retailer_id": "R-04",
        "sku":         "SKU-004",
        "po_price":    16.0,
        "sap_price":   18.0,
        "line_count":  1,
        "metadata":    json.dumps({
            "signal_scores": {
                "po_number":     1.0,
                "customer_id":   1.0,
                "line_items":    1.0,
                "amount":        1.0,
                "timestamp":     0.8,
                "ship_to":       1.0,
                "channel":       1.0,
                "delivery_date": 1.0,
            },
        }),
    },
    # ── DUPLICATE_PO — composite score 0.65 → SOFT_FLAG ───────────────────
    {
        "event_id":    "EVT-DPO-002",
        "event_type":  "EDI_850_DUPLICATE_PO",
        "order_id":    "PO-9002",
        "retailer_id": "R-04",
        "sku":         "SKU-004",
        "po_price":    16.0,
        "sap_price":   18.0,
        "line_count":  1,
        "metadata":    json.dumps({
            "signal_scores": {
                "po_number":     1.0,
                "customer_id":   1.0,
                "line_items":    0.5,
                "amount":        0.5,
                "timestamp":     0.0,
                "ship_to":       0.5,
                "channel":       0.5,
                "delivery_date": 0.0,
            },
        }),
    },
]


# ---------------------------------------------------------------------------
# Seeder
# ---------------------------------------------------------------------------

def seed(db_path: Path, reset: bool = False) -> None:
    """Create tables and insert sample data into *db_path*."""
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if reset and db_path.exists():
        db_path.unlink()
        print(f"🗑  Removed existing database: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    # SAP pricing
    conn.executemany(
        "INSERT OR REPLACE INTO sap_pricing "
        "  (sku, description, base_price, currency, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [(sku, desc, price, cur, _NOW) for sku, desc, price, cur in _SAP_PRICING],
    )

    # Retailer contracts
    conn.executemany(
        "INSERT OR REPLACE INTO retailer_contracts "
        "  (retailer_id, sku, contract_price, discount_pct, contract_start, contract_end) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(rid, sku, cp, dp, _CONTRACT_START, _CONTRACT_END)
         for rid, sku, cp, dp in _RETAILER_CONTRACTS],
    )

    # Credit profiles
    conn.executemany(
        "INSERT OR REPLACE INTO credit_profiles "
        "  (retailer_id, credit_limit, current_exposure, updated_at) "
        "VALUES (?, ?, ?, ?)",
        [(rid, cl, ce, _NOW) for rid, cl, ce, in _CREDIT_PROFILES],
    )

    # EDI events
    for ev in _EDI_EVENTS:
        conn.execute(
            "INSERT OR REPLACE INTO edi_events "
            "  (event_id, event_type, order_id, retailer_id, sku, "
            "   po_price, sap_price, line_count, metadata, created_at) "
            "VALUES "
            "  (:event_id, :event_type, :order_id, :retailer_id, :sku, "
            "   :po_price, :sap_price, :line_count, :metadata, :created_at)",
            {**ev, "created_at": _NOW},
        )

    conn.commit()
    conn.close()

    print(f"✅  Seeded: {db_path}")
    print(f"    {len(_SAP_PRICING)} SKUs")
    print(f"    {len(_RETAILER_CONTRACTS)} retailer contracts")
    print(f"    {len(_CREDIT_PROFILES)} credit profiles")
    print(f"    {len(_EDI_EVENTS)} EDI events")
    print()
    _print_event_summary()


def _print_event_summary() -> None:
    print("EDI event summary:")
    intent_labels = {
        "EVT-CC-":  "CONTRACTUAL_CORRECTION",
        "EVT-CB-":  "CREDIT_BLOCK",
        "EVT-MPE-": "MASS_PRICING_ERROR",
        "EVT-DPO-": "DUPLICATE_PO",
    }
    for ev in _EDI_EVENTS:
        label = next(
            (v for k, v in intent_labels.items() if ev["event_id"].startswith(k)),
            "UNKNOWN",
        )
        print(f"  {ev['event_id']:15s}  {label:28s}  {ev['order_id']}")


def load_events(db_path: Path) -> list[dict]:
    """Return all EDI events from the database as dicts (for the UI)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM edi_events ORDER BY id"
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
