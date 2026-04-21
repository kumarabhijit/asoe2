"""Sandbox SQLite seeder.

Populates a local sandbox.db with sample SAP pricing data, retailer contracts,
credit profiles, and EDI 850 events that exercise all four supported intents:
  CONTRACTUAL_CORRECTION, CREDIT_BLOCK, MASS_PRICING_ERROR, DUPLICATE_PO

Usage
-----
    python tests/sandbox/seed.py                      # -> tests/sandbox/sandbox.db
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

-- Pre-loaded EDI 850 events covering all four supported intents.
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

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

_NOW = datetime.now(timezone.utc).isoformat()
_CONTRACT_START = "2025-01-01"
_CONTRACT_END   = "2026-12-31"

# ---- Customers (10 retailers) -------------------------------------------

_CUSTOMERS = [
    # (retailer_id, name, region, tier)
    ("R-01", "MegaMart Inc.",          "US-EAST",    "PREMIUM"),
    ("R-02", "ValueShop Corp.",        "US-EAST",    "STANDARD"),
    ("R-03", "BudgetBuy LLC",          "US-SOUTH",   "BASIC"),
    ("R-04", "QuickStop Retail",       "US-MIDWEST",  "STANDARD"),
    ("R-05", "CleanWorld Stores",      "US-WEST",    "PREMIUM"),
    ("R-06", "FreshChoice Markets",    "US-EAST",    "STANDARD"),
    ("R-07", "SaveMore Distributors",  "US-SOUTH",   "BASIC"),
    ("R-08", "PrimeGoods Ltd.",        "US-WEST",    "PREMIUM"),
    ("R-09", "AllDay Convenience",     "US-MIDWEST",  "STANDARD"),
    ("R-10", "EcoSupply Co.",          "US-EAST",    "BASIC"),
]

# ---- Distribution Centers (5 DCs) ---------------------------------------

_DISTRIBUTION_CENTERS = [
    # (dc_id, name, region, capacity_pct)
    ("DC-EAST-01",    "East Coast Hub",       "US-EAST",     85.0),
    ("DC-SOUTH-01",   "Southern Warehouse",   "US-SOUTH",    72.0),
    ("DC-MIDWEST-01", "Midwest Fulfillment",  "US-MIDWEST",  90.0),
    ("DC-WEST-01",    "West Coast Center",    "US-WEST",     68.0),
    ("DC-WEST-02",    "Pacific Overflow",     "US-WEST",     45.0),
]

# ---- SAP Pricing (10 SKUs) -----------------------------------------------

_SAP_PRICING = [
    # (sku, description, category, base_price, currency, dc_id)
    ("SKU-001", "Premium Detergent 5L",    "CLEANING",   100.00, "USD", "DC-EAST-01"),
    ("SKU-002", "Budget Detergent 2L",     "CLEANING",    45.00, "USD", "DC-EAST-01"),
    ("SKU-003", "Fabric Softener 3L",      "CLEANING",    62.00, "USD", "DC-SOUTH-01"),
    ("SKU-004", "Dish Soap 1L",            "CLEANING",    18.00, "USD", "DC-MIDWEST-01"),
    ("SKU-005", "All-Purpose Cleaner 4L",  "CLEANING",    55.00, "USD", "DC-WEST-01"),
    ("SKU-006", "Laundry Pods 60ct",       "CLEANING",    34.00, "USD", "DC-EAST-01"),
    ("SKU-007", "Glass Cleaner 750ml",     "CLEANING",    12.50, "USD", "DC-SOUTH-01"),
    ("SKU-008", "Floor Polish 2L",         "CLEANING",    28.00, "USD", "DC-MIDWEST-01"),
    ("SKU-009", "Hand Soap Refill 1L",     "PERSONAL",    15.00, "USD", "DC-WEST-01"),
    ("SKU-010", "Stain Remover 500ml",     "CLEANING",    22.00, "USD", "DC-EAST-01"),
]

# ---- Retailer Contracts (15 contracts) ------------------------------------

_RETAILER_CONTRACTS = [
    # (retailer_id, sku, contract_price, discount_pct)
    # R-01 MegaMart — PREMIUM tier, negotiated discounts
    ("R-01", "SKU-001",  90.00, 10.0),   # 10% — within 15% threshold
    ("R-01", "SKU-002",  40.50, 10.0),
    ("R-01", "SKU-006",  29.00, 14.7),   # near threshold edge
    # R-02 ValueShop — STANDARD tier
    ("R-02", "SKU-001",  92.00,  8.0),   # 8% — within threshold
    ("R-02", "SKU-003",  55.80, 10.0),
    # R-03 BudgetBuy — aggressive discount, will exceed threshold
    ("R-03", "SKU-001",  78.00, 22.0),   # 22% — EXCEEDS 15% threshold -> FAILED
    ("R-03", "SKU-007",  10.00, 20.0),   # 20% — also exceeds
    # R-04 QuickStop
    ("R-04", "SKU-004",  16.00, 11.1),
    ("R-04", "SKU-008",  25.20, 10.0),
    # R-05 CleanWorld — PREMIUM
    ("R-05", "SKU-005",  52.25,  5.0),
    ("R-05", "SKU-009",  13.50, 10.0),
    # R-06 FreshChoice
    ("R-06", "SKU-001",  88.00, 12.0),   # 12% — within threshold
    ("R-06", "SKU-010",  19.80, 10.0),
    # R-08 PrimeGoods — PREMIUM
    ("R-08", "SKU-001",  87.00, 13.0),   # 13% — within threshold, close to edge
    ("R-08", "SKU-003",  54.00, 12.9),
]

# ---- Promotions (seasonal / regional) ------------------------------------

_PROMOTIONS = [
    # (promo_id, sku, promo_type, discount_pct, start_date, end_date, region)
    ("PROMO-2025-Q1-A", "SKU-001", "SEASONAL",  5.0, "2025-01-01", "2025-03-31", "US-EAST"),
    ("PROMO-2025-Q1-B", "SKU-006", "CLEARANCE", 15.0, "2025-02-01", "2025-04-30", None),
    ("PROMO-2025-Q2-A", "SKU-003", "SEASONAL",  8.0, "2025-04-01", "2025-06-30", "US-SOUTH"),
    ("PROMO-2025-SPR",  "SKU-010", "BUNDLE",   10.0, "2025-03-01", "2025-05-31", None),
]

# ---- Credit Profiles (8 retailers with credit data) ----------------------

_CREDIT_PROFILES = [
    # (retailer_id, credit_limit, current_exposure, risk_rating, last_review_date)
    ("R-01", 10_000.00, 10_100.00, "WATCH",    "2025-03-01"),  # over by $100 — within $5k tolerance
    ("R-02", 15_000.00, 14_600.00, "NORMAL",   "2025-03-10"),  # under limit — healthy
    ("R-03",  5_000.00,  4_800.00, "NORMAL",   "2025-02-15"),  # under limit — healthy
    ("R-05", 20_000.00, 25_500.00, "HIGH",     "2025-03-15"),  # OVER by $5,500 -> REJECTED
    ("R-06", 12_000.00, 11_800.00, "NORMAL",   "2025-03-12"),  # gap $200 — healthy
    ("R-07",  3_000.00,  7_500.00, "HIGH",     "2025-01-20"),  # OVER by $4,500 — within tolerance
    ("R-08", 50_000.00, 35_000.00, "NORMAL",   "2025-03-18"),  # well under limit
    ("R-10",  8_000.00, 13_200.00, "HIGH",     "2025-02-28"),  # OVER by $5,200 -> REJECTED
]

# ---- EDI Events (20 events) -----------------------------------------------

_EDI_EVENTS = [
    # ======================================================================
    # CONTRACTUAL_CORRECTION — price mismatch within/beyond 15% discount
    # ======================================================================

    # CC-001: 10% discount, within threshold -> GREEN -> COMPLETE
    {
        "event_id":    "EVT-CC-001",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-1001",
        "retailer_id": "R-01",
        "sku":         "SKU-001",
        "po_price":    90.0,
        "sap_price":   100.0,
        "line_count":  1,
        "dc_id":       "DC-EAST-01",
        "metadata":    json.dumps({"contract_ref": "CTR-R01-2025", "note": "10% contract discount"}),
    },
    # CC-002: 8% discount, within threshold -> GREEN -> COMPLETE
    {
        "event_id":    "EVT-CC-002",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-1002",
        "retailer_id": "R-02",
        "sku":         "SKU-001",
        "po_price":    92.0,
        "sap_price":   100.0,
        "line_count":  1,
        "dc_id":       "DC-EAST-01",
        "metadata":    json.dumps({"contract_ref": "CTR-R02-2025", "note": "8% contract discount"}),
    },
    # CC-003: 22% discount, EXCEEDS threshold -> GREEN but recipe returns FAILED
    {
        "event_id":    "EVT-CC-003",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-1003",
        "retailer_id": "R-03",
        "sku":         "SKU-001",
        "po_price":    78.0,
        "sap_price":   100.0,
        "line_count":  1,
        "dc_id":       "DC-SOUTH-01",
        "metadata":    json.dumps({"contract_ref": "CTR-R03-2025", "discount_claimed": "22%"}),
    },
    # CC-004: 12% discount (R-06), within threshold -> GREEN -> COMPLETE
    {
        "event_id":    "EVT-CC-004",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-1004",
        "retailer_id": "R-06",
        "sku":         "SKU-001",
        "po_price":    88.0,
        "sap_price":   100.0,
        "line_count":  1,
        "dc_id":       "DC-EAST-01",
        "metadata":    json.dumps({"contract_ref": "CTR-R06-2025", "note": "12% contract discount"}),
    },
    # CC-005: 13% discount (R-08), within threshold, close to edge -> COMPLETE
    {
        "event_id":    "EVT-CC-005",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-1005",
        "retailer_id": "R-08",
        "sku":         "SKU-001",
        "po_price":    87.0,
        "sap_price":   100.0,
        "line_count":  1,
        "dc_id":       "DC-WEST-01",
        "metadata":    json.dumps({"contract_ref": "CTR-R08-2025", "note": "13% PREMIUM discount"}),
    },
    # CC-006: 14.7% discount (R-01 SKU-006), within threshold, edge case
    {
        "event_id":    "EVT-CC-006",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-1006",
        "retailer_id": "R-01",
        "sku":         "SKU-006",
        "po_price":    29.0,
        "sap_price":   34.0,
        "line_count":  1,
        "dc_id":       "DC-EAST-01",
        "metadata":    json.dumps({"contract_ref": "CTR-R01-2025", "note": "14.7% near-edge discount"}),
    },
    # CC-007: 20% discount (R-03 SKU-007), EXCEEDS threshold -> FAILED
    {
        "event_id":    "EVT-CC-007",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-1007",
        "retailer_id": "R-03",
        "sku":         "SKU-007",
        "po_price":    10.0,
        "sap_price":   12.5,
        "line_count":  1,
        "dc_id":       "DC-SOUTH-01",
        "metadata":    json.dumps({"contract_ref": "CTR-R03-2025", "discount_claimed": "20%"}),
    },

    # ======================================================================
    # CREDIT_BLOCK — credit hold/release scenarios
    # ======================================================================

    # CB-001: ORDER_MANAGER, $100 over limit (within $5k tolerance) -> YELLOW -> MANUAL_REVIEW
    {
        "event_id":    "EVT-CB-001",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-2001",
        "retailer_id": "R-01",
        "sku":         "SKU-002",
        "po_price":    100.0,
        "sap_price":   100.0,
        "line_count":  1,
        "dc_id":       "DC-EAST-01",
        "metadata":    json.dumps({
            "requester_role":    "ORDER_MANAGER",
            "credit_limit":      10000.0,
            "current_exposure":  10100.0,
        }),
    },
    # CB-002: FINANCE_DIRECTOR, $5,500 over (exceeds $5k tolerance) -> YELLOW -> REJECTED
    {
        "event_id":    "EVT-CB-002",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-2002",
        "retailer_id": "R-05",
        "sku":         "SKU-005",
        "po_price":    100.0,
        "sap_price":   100.0,
        "line_count":  1,
        "dc_id":       "DC-WEST-01",
        "metadata":    json.dumps({
            "requester_role":    "FINANCE_DIRECTOR",
            "credit_limit":      20000.0,
            "current_exposure":  25500.0,
            "note":              "exposure $5,500 over limit - rejected",
        }),
    },
    # CB-003: ORDER_MANAGER, $4,500 over (R-07, within tolerance) -> YELLOW -> MANUAL_REVIEW
    {
        "event_id":    "EVT-CB-003",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-2003",
        "retailer_id": "R-07",
        "sku":         "SKU-004",
        "po_price":    18.0,
        "sap_price":   18.0,
        "line_count":  1,
        "dc_id":       "DC-SOUTH-01",
        "metadata":    json.dumps({
            "requester_role":    "ORDER_MANAGER",
            "credit_limit":      3000.0,
            "current_exposure":  7500.0,
            "note":              "$4,500 over limit - within $5k tolerance",
        }),
    },
    # CB-004: FINANCE_DIRECTOR, $5,200 over (R-10, exceeds tolerance) -> YELLOW -> REJECTED
    {
        "event_id":    "EVT-CB-004",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-2004",
        "retailer_id": "R-10",
        "sku":         "SKU-010",
        "po_price":    22.0,
        "sap_price":   22.0,
        "line_count":  1,
        "dc_id":       "DC-EAST-01",
        "metadata":    json.dumps({
            "requester_role":    "FINANCE_DIRECTOR",
            "credit_limit":      8000.0,
            "current_exposure":  13200.0,
            "note":              "$5,200 over limit - exceeds tolerance, rejected",
        }),
    },

    # ======================================================================
    # MASS_PRICING_ERROR — line_count > 10, shadow RED -> BLOCKED
    # ======================================================================

    # MPE-001: 15 lines, systemic risk -> RED -> BLOCKED
    {
        "event_id":    "EVT-MPE-001",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-3001",
        "retailer_id": "R-02",
        "sku":         "SKU-003",
        "po_price":    50.0,
        "sap_price":   62.0,
        "line_count":  15,
        "dc_id":       "DC-SOUTH-01",
        "metadata":    json.dumps({
            "batch_ref": "BATCH-2025-01",
            "note":      "15-line batch - systemic risk",
        }),
    },
    # MPE-002: 25 lines, large batch -> RED -> BLOCKED
    {
        "event_id":    "EVT-MPE-002",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-3002",
        "retailer_id": "R-06",
        "sku":         "SKU-001",
        "po_price":    85.0,
        "sap_price":   100.0,
        "line_count":  25,
        "dc_id":       "DC-EAST-01",
        "metadata":    json.dumps({
            "batch_ref": "BATCH-2025-02",
            "note":      "25-line batch - major systemic risk",
        }),
    },
    # MPE-003: 11 lines, just over threshold -> RED -> BLOCKED
    {
        "event_id":    "EVT-MPE-003",
        "event_type":  "EDI_850_PRICE_MISMATCH",
        "order_id":    "SO-3003",
        "retailer_id": "R-08",
        "sku":         "SKU-009",
        "po_price":    13.0,
        "sap_price":   15.0,
        "line_count":  11,
        "dc_id":       "DC-WEST-01",
        "metadata":    json.dumps({
            "batch_ref": "BATCH-2025-03",
            "note":      "11 lines - just over threshold",
        }),
    },

    # ======================================================================
    # DUPLICATE_PO — composite score determines action
    # ======================================================================

    # DPO-001: composite 0.98 -> AUTO_BLOCK (all signals near 1.0)
    {
        "event_id":    "EVT-DPO-001",
        "event_type":  "EDI_850_DUPLICATE_PO",
        "order_id":    "PO-9001",
        "retailer_id": "R-04",
        "sku":         "SKU-004",
        "po_price":    16.0,
        "sap_price":   18.0,
        "line_count":  1,
        "dc_id":       "DC-MIDWEST-01",
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
    # DPO-002: composite 0.65 -> SOFT_FLAG (mixed signals)
    {
        "event_id":    "EVT-DPO-002",
        "event_type":  "EDI_850_DUPLICATE_PO",
        "order_id":    "PO-9002",
        "retailer_id": "R-04",
        "sku":         "SKU-004",
        "po_price":    16.0,
        "sap_price":   18.0,
        "line_count":  1,
        "dc_id":       "DC-MIDWEST-01",
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
    # DPO-003: composite 0.82 -> REVIEW_REQUIRED (high but below auto-block)
    {
        "event_id":    "EVT-DPO-003",
        "event_type":  "EDI_850_DUPLICATE_PO",
        "order_id":    "PO-9003",
        "retailer_id": "R-09",
        "sku":         "SKU-008",
        "po_price":    25.20,
        "sap_price":   28.0,
        "line_count":  1,
        "dc_id":       "DC-MIDWEST-01",
        "metadata":    json.dumps({
            "signal_scores": {
                "po_number":     1.0,
                "customer_id":   1.0,
                "line_items":    0.8,
                "amount":        0.9,
                "timestamp":     0.5,
                "ship_to":       0.7,
                "channel":       0.6,
                "delivery_date": 0.8,
            },
        }),
    },
    # DPO-004: composite 0.40 -> CLEAR (below soft_flag threshold 0.50)
    {
        "event_id":    "EVT-DPO-004",
        "event_type":  "EDI_850_DUPLICATE_PO",
        "order_id":    "PO-9004",
        "retailer_id": "R-06",
        "sku":         "SKU-010",
        "po_price":    19.80,
        "sap_price":   22.0,
        "line_count":  1,
        "dc_id":       "DC-EAST-01",
        "metadata":    json.dumps({
            "signal_scores": {
                "po_number":     1.0,
                "customer_id":   0.5,
                "line_items":    0.0,
                "amount":        0.3,
                "timestamp":     0.0,
                "ship_to":       0.0,
                "channel":       0.5,
                "delivery_date": 0.0,
            },
        }),
    },

    # ---- EC04: Exact resend — Costco PO bounced, resent same order --------
    # DPO-005: composite 0.92 -> AUTO_BLOCK (all signals high, timestamp lower)
    {
        "event_id":    "EVT-DPO-005",
        "event_type":  "EDI_850_DUPLICATE_PO",
        "order_id":    "PO-88424",
        "retailer_id": "R-05",
        "sku":         "SKU-005",
        "po_price":    36.0,
        "sap_price":   36.0,
        "line_count":  2,
        "dc_id":       "DC-WEST-01",
        "metadata":    json.dumps({
            "signal_scores": {
                "po_number":     1.0,
                "customer_id":   1.0,
                "line_items":    1.0,
                "amount":        1.0,
                "timestamp":     0.3,
                "ship_to":       1.0,
                "channel":       1.0,
                "delivery_date": 1.0,
            },
            "note": "EC04 — exact resend (first email bounced)",
        }),
    },

    # ---- EC08: Multiple POs in single batch email — Amazon weekly batch ---
    # DPO-006: 1st PO in batch, exact match -> AUTO_BLOCK
    {
        "event_id":    "EVT-DPO-006",
        "event_type":  "EDI_850_DUPLICATE_PO",
        "order_id":    "PO-AMZ-001",
        "retailer_id": "R-08",
        "sku":         "SKU-001",
        "po_price":    14.88,
        "sap_price":   14.88,
        "line_count":  2,
        "dc_id":       "DC-WEST-01",
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
            "source_email_id": "EC08-multiple-pos-amazon",
            "batch_po_index":  1,
            "note": "EC08 — batch PO 1 of 3, exact match in system",
        }),
    },
    # DPO-007: 2nd PO in batch, partial match -> SOFT_FLAG
    {
        "event_id":    "EVT-DPO-007",
        "event_type":  "EDI_850_DUPLICATE_PO",
        "order_id":    "PO-AMZ-002",
        "retailer_id": "R-08",
        "sku":         "SKU-003",
        "po_price":    9.60,
        "sap_price":   9.60,
        "line_count":  2,
        "dc_id":       "DC-SOUTH-01",
        "metadata":    json.dumps({
            "signal_scores": {
                "po_number":     0.8,
                "customer_id":   1.0,
                "line_items":    0.4,
                "amount":        0.3,
                "timestamp":     0.0,
                "ship_to":       0.5,
                "channel":       1.0,
                "delivery_date": 0.5,
            },
            "source_email_id": "EC08-multiple-pos-amazon",
            "batch_po_index":  2,
            "note": "EC08 — batch PO 2 of 3, partial match",
        }),
    },
    # DPO-008: 3rd PO in batch, no match -> PASS
    {
        "event_id":    "EVT-DPO-008",
        "event_type":  "EDI_850_DUPLICATE_PO",
        "order_id":    "PO-AMZ-003",
        "retailer_id": "R-08",
        "sku":         "SKU-009",
        "po_price":    8.96,
        "sap_price":   8.96,
        "line_count":  1,
        "dc_id":       "DC-WEST-02",
        "metadata":    json.dumps({
            "signal_scores": {
                "po_number":     0.0,
                "customer_id":   1.0,
                "line_items":    0.0,
                "amount":        0.0,
                "timestamp":     0.0,
                "ship_to":       0.0,
                "channel":       1.0,
                "delivery_date": 0.0,
            },
            "source_email_id": "EC08-multiple-pos-amazon",
            "batch_po_index":  3,
            "note": "EC08 — batch PO 3 of 3, no existing match",
        }),
    },

    # ======================================================================
    # PRICE_HOLD_RELEASE — variance vs tolerance/hard_block thresholds
    # ======================================================================

    # PHR-001: 1% variance -> AUTO_RELEASE (within 2% tolerance)
    {
        "event_id":    "EVT-PHR-001",
        "event_type":  "EDI_850_PRICE_HOLD",
        "order_id":    "PO-PHR-001",
        "retailer_id": "R-01",
        "sku":         "SKU-101",
        "po_price":    101.0,
        "sap_price":   100.0,
        "line_count":  1,
        "dc_id":       "DC-EAST-01",
        "metadata":    json.dumps({
            "price_hold_status": "HELD",
            "note": "1% variance — auto-release branch",
        }),
    },
    # PHR-002: 5% variance -> ESCALATE (tolerance < variance <= hard_block)
    {
        "event_id":    "EVT-PHR-002",
        "event_type":  "EDI_850_PRICE_HOLD",
        "order_id":    "PO-PHR-002",
        "retailer_id": "R-02",
        "sku":         "SKU-102",
        "po_price":    105.0,
        "sap_price":   100.0,
        "line_count":  1,
        "dc_id":       "DC-WEST-01",
        "metadata":    json.dumps({
            "price_hold_status": "HELD",
            "note": "5% variance — manager escalation branch",
        }),
    },
    # PHR-003: 15% variance -> HARD_BLOCK (exceeds hard_block ceiling)
    {
        "event_id":    "EVT-PHR-003",
        "event_type":  "EDI_850_PRICE_HOLD",
        "order_id":    "PO-PHR-003",
        "retailer_id": "R-03",
        "sku":         "SKU-103",
        "po_price":    115.0,
        "sap_price":   100.0,
        "line_count":  1,
        "dc_id":       "DC-MIDWEST-01",
        "metadata":    json.dumps({
            "price_hold_status": "HELD",
            "note": "15% variance — hard-block / admin-release branch",
        }),
    },

    # ======================================================================
    # EDI_MISMATCH — sub_type-driven classification
    # Different retailers per event to stress by_intent × by_tenant stats.
    # ======================================================================

    # EDM-001: SKU mismatch -> HARD_REJECT
    {
        "event_id":    "EVT-EDM-001",
        "event_type":  "EDI_850_LINE_MISMATCH",
        "order_id":    "PO-EDM-001",
        "retailer_id": "R-01",
        "sku":         "SKU-201",
        "po_price":    100.0,
        "sap_price":   100.0,
        "line_count":  1,
        "dc_id":       "DC-EAST-01",
        "metadata":    json.dumps({
            "mismatch_sub_type": "SKU_MISMATCH",
            "expected_value":    "SKU-201",
            "received_value":    "SKU-999",
        }),
    },
    # EDM-002: Quantity mismatch -> REVIEW
    {
        "event_id":    "EVT-EDM-002",
        "event_type":  "EDI_850_LINE_MISMATCH",
        "order_id":    "PO-EDM-002",
        "retailer_id": "R-02",
        "sku":         "SKU-202",
        "po_price":    50.0,
        "sap_price":   50.0,
        "line_count":  1,
        "dc_id":       "DC-WEST-01",
        "metadata":    json.dumps({
            "mismatch_sub_type": "QTY_MISMATCH",
            "expected_value":    120,
            "received_value":    144,
        }),
    },
    # EDM-003: UoM mismatch -> REVIEW
    {
        "event_id":    "EVT-EDM-003",
        "event_type":  "EDI_850_LINE_MISMATCH",
        "order_id":    "PO-EDM-003",
        "retailer_id": "R-03",
        "sku":         "SKU-203",
        "po_price":    12.0,
        "sap_price":   12.0,
        "line_count":  1,
        "dc_id":       "DC-MIDWEST-01",
        "metadata":    json.dumps({
            "mismatch_sub_type": "UOM_MISMATCH",
            "expected_value":    "EA",
            "received_value":    "CS",
        }),
    },
    # EDM-004: Ship-to mismatch -> ESCALATE
    {
        "event_id":    "EVT-EDM-004",
        "event_type":  "EDI_850_LINE_MISMATCH",
        "order_id":    "PO-EDM-004",
        "retailer_id": "R-01",
        "sku":         "SKU-204",
        "po_price":    80.0,
        "sap_price":   80.0,
        "line_count":  1,
        "dc_id":       "DC-EAST-01",
        "metadata":    json.dumps({
            "mismatch_sub_type": "SHIP_TO_MISMATCH",
            "expected_value":    "W-01",
            "received_value":    "W-02",
        }),
    },
    # EDM-005: PRICE_MISMATCH -> re-routed by classifier to
    # CONTRACTUAL_CORRECTION / PriceAdjustmentRecipe.py (pricing single
    # source of truth, CLAUDE.md §1). Lands as CONTRACTUAL_CORRECTION, NOT
    # EDI_MISMATCH — useful to demonstrate the routing fork in the sandbox.
    {
        "event_id":    "EVT-EDM-005",
        "event_type":  "EDI_850_LINE_MISMATCH",
        "order_id":    "PO-EDM-005",
        "retailer_id": "R-02",
        "sku":         "SKU-205",
        "po_price":    95.0,
        "sap_price":   100.0,
        "line_count":  1,
        "dc_id":       "DC-WEST-01",
        "metadata":    json.dumps({
            "mismatch_sub_type": "PRICE_MISMATCH",
            "expected_value":    100.0,
            "received_value":    95.0,
            "note": "Routes to CONTRACTUAL_CORRECTION at classifier time.",
        }),
    },
    # EDM-006: invalid sub_type -> recipe returns FAILED -> FAIL_TO_HUMAN.
    # Demonstrates the constrained-generation guarantee at the sandbox level.
    {
        "event_id":    "EVT-EDM-006",
        "event_type":  "EDI_850_LINE_MISMATCH",
        "order_id":    "PO-EDM-006",
        "retailer_id": "R-03",
        "sku":         "SKU-206",
        "po_price":    40.0,
        "sap_price":   40.0,
        "line_count":  1,
        "dc_id":       "DC-MIDWEST-01",
        "metadata":    json.dumps({
            "mismatch_sub_type": "BAD_VALUE_NOT_IN_LITERAL",
            "expected_value":    "foo",
            "received_value":    "bar",
            "note": "Exercises pydantic-validation FAIL_TO_HUMAN path.",
        }),
    },

    # ======================================================================
    # BACK_ORDER — gap_pct vs severe threshold
    # ======================================================================
    {
        "event_id":    "EVT-BO-001",
        "event_type":  "BACK_ORDER_OOS",
        "order_id":    "PO-BO-001",
        "retailer_id": "R-01",
        "sku":         "SKU-301",
        "po_price":    12.50,
        "sap_price":   12.50,
        "line_count":  1,
        "dc_id":       "DC-EAST-01",
        "metadata":    json.dumps({
            "ordered_qty": 100, "available_qty": 75,
            "unit_price": 12.50, "uom": "CS",
            "note": "25% gap → MINOR_GAP / YELLOW.",
        }),
    },
    {
        "event_id":    "EVT-BO-002",
        "event_type":  "BACK_ORDER_OOS",
        "order_id":    "PO-BO-002",
        "retailer_id": "R-02",
        "sku":         "SKU-302",
        "po_price":    8.00,
        "sap_price":   8.00,
        "line_count":  1,
        "dc_id":       "DC-WEST-01",
        "metadata":    json.dumps({
            "ordered_qty": 200, "available_qty": 60,
            "unit_price": 8.00, "uom": "CS",
            "note": "70% gap → SEVERE_GAP / RED.",
        }),
    },

    # ======================================================================
    # OVER_MAX — exceedance vs severe threshold
    # ======================================================================
    {
        "event_id":    "EVT-OM-001",
        "event_type":  "OVER_MAX_QTY",
        "order_id":    "PO-OM-001",
        "retailer_id": "R-03",
        "sku":         "SKU-401",
        "po_price":    4.00,
        "sap_price":   4.00,
        "line_count":  1,
        "dc_id":       "DC-MIDWEST-01",
        "metadata":    json.dumps({
            "total_ordered": 130, "max_qty": 100,
            "order_lines": [
                {"sku": "SKU-401", "description": "SKU-401", "qty": 130,
                 "max_line_qty": 100, "is_even_layer_item": True},
            ],
            "note": "30% exceedance → MINOR / YELLOW.",
        }),
    },
    {
        "event_id":    "EVT-OM-002",
        "event_type":  "OVER_MAX_QTY",
        "order_id":    "PO-OM-002",
        "retailer_id": "R-01",
        "sku":         "SKU-402",
        "po_price":    6.00,
        "sap_price":   6.00,
        "line_count":  1,
        "dc_id":       "DC-EAST-01",
        "metadata":    json.dumps({
            "total_ordered": 170, "max_qty": 100,
            "order_lines": [
                {"sku": "SKU-402", "description": "SKU-402", "qty": 170,
                 "max_line_qty": 100, "is_even_layer_item": True},
            ],
            "note": "70% exceedance → SEVERE / RED.",
        }),
    },

    # ======================================================================
    # MIN_ORDER_QTY — shortfall vs severe threshold
    # ======================================================================
    {
        "event_id":    "EVT-MOQ-001",
        "event_type":  "MIN_ORDER_QTY",
        "order_id":    "PO-MOQ-001",
        "retailer_id": "R-02",
        "sku":         "SKU-501",
        "po_price":    5.00,
        "sap_price":   5.00,
        "line_count":  1,
        "dc_id":       "DC-WEST-01",
        "metadata":    json.dumps({
            "ordered_qty": 40, "moq_qty": 48,
            "unit_cost": 5.00, "uom": "CS",
            "note": "~17% shortfall → MINOR / YELLOW, round-up to 48.",
        }),
    },
    {
        "event_id":    "EVT-MOQ-002",
        "event_type":  "MIN_ORDER_QTY",
        "order_id":    "PO-MOQ-002",
        "retailer_id": "R-03",
        "sku":         "SKU-502",
        "po_price":    12.00,
        "sap_price":   12.00,
        "line_count":  1,
        "dc_id":       "DC-MIDWEST-01",
        "metadata":    json.dumps({
            "ordered_qty": 25, "moq_qty": 48,
            "unit_cost": 12.00, "uom": "CS",
            "note": "~48% shortfall → SEVERE / RED, KNMT waiver required.",
        }),
    },

    # ======================================================================
    # PALLET_CONFIG — broken layer
    # ======================================================================
    {
        "event_id":    "EVT-PLT-001",
        "event_type":  "PALLET_CONFIG_VIOLATION",
        "order_id":    "PO-PLT-001",
        "retailer_id": "R-01",
        "sku":         "SKU-601",
        "po_price":    9.00,
        "sap_price":   9.00,
        "line_count":  1,
        "dc_id":       "DC-EAST-01",
        "metadata":    json.dumps({
            "pallet_lines": [
                {"sku": "SKU-601", "description": "SKU-601",
                 "layer_qty": 24, "pallet_qty": 96,
                 "ordered_qty": 100, "uom": "CS"},
            ],
            "note": "100 cases → 1 pallet + 4 loose → BROKEN_LAYER / YELLOW.",
        }),
    },

    # ======================================================================
    # DELIVERY_DELAY — minor + severe
    # ======================================================================
    {
        "event_id":    "EVT-DD-001",
        "event_type":  "DELIVERY_DELAY",
        "order_id":    "PO-DD-001",
        "retailer_id": "R-02",
        "sku":         "SKU-701",
        "po_price":    15.00,
        "sap_price":   15.00,
        "line_count":  1,
        "dc_id":       "DC-WEST-01",
        "metadata":    json.dumps({
            "planned_date": "2026-04-20T00:00:00Z",
            "projected_eta": "2026-04-23T00:00:00Z",
            "days_late": 3,
            "delay_category": "CARRIER_DELAY",
            "note": "3 days late → MINOR_DELAY / YELLOW.",
        }),
    },
    {
        "event_id":    "EVT-DD-002",
        "event_type":  "DELIVERY_DELAY",
        "order_id":    "PO-DD-002",
        "retailer_id": "R-03",
        "sku":         "SKU-702",
        "po_price":    20.00,
        "sap_price":   20.00,
        "line_count":  1,
        "dc_id":       "DC-MIDWEST-01",
        "metadata":    json.dumps({
            "planned_date": "2026-04-20T00:00:00Z",
            "projected_eta": "2026-04-27T00:00:00Z",
            "days_late": 7,
            "delay_category": "CUSTOMS",
            "note": "7 days late → SEVERE_DELAY / RED.",
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
        print(f"Removed existing database: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    # Customers
    conn.executemany(
        "INSERT OR REPLACE INTO customers "
        "  (retailer_id, name, region, tier, active, created_at) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        [(rid, name, region, tier, _NOW) for rid, name, region, tier in _CUSTOMERS],
    )

    # Distribution Centers
    conn.executemany(
        "INSERT OR REPLACE INTO distribution_centers "
        "  (dc_id, name, region, capacity_pct, active) "
        "VALUES (?, ?, ?, ?, 1)",
        _DISTRIBUTION_CENTERS,
    )

    # SAP pricing
    conn.executemany(
        "INSERT OR REPLACE INTO sap_pricing "
        "  (sku, description, category, base_price, currency, dc_id, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(sku, desc, cat, price, cur, dc, _NOW)
         for sku, desc, cat, price, cur, dc in _SAP_PRICING],
    )

    # Retailer contracts
    conn.executemany(
        "INSERT OR REPLACE INTO retailer_contracts "
        "  (retailer_id, sku, contract_price, discount_pct, contract_start, contract_end) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(rid, sku, cp, dp, _CONTRACT_START, _CONTRACT_END)
         for rid, sku, cp, dp in _RETAILER_CONTRACTS],
    )

    # Promotions
    conn.executemany(
        "INSERT OR REPLACE INTO promotions "
        "  (promo_id, sku, promo_type, discount_pct, start_date, end_date, region, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        _PROMOTIONS,
    )

    # Credit profiles
    conn.executemany(
        "INSERT OR REPLACE INTO credit_profiles "
        "  (retailer_id, credit_limit, current_exposure, risk_rating, last_review_date, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(rid, cl, ce, rr, lrd, _NOW)
         for rid, cl, ce, rr, lrd in _CREDIT_PROFILES],
    )

    # EDI events
    for ev in _EDI_EVENTS:
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
    print(f"    {len(_CUSTOMERS)} customers")
    print(f"    {len(_DISTRIBUTION_CENTERS)} distribution centers")
    print(f"    {len(_SAP_PRICING)} SKUs")
    print(f"    {len(_RETAILER_CONTRACTS)} retailer contracts")
    print(f"    {len(_PROMOTIONS)} promotions")
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


def load_customers(db_path: Path) -> list[dict]:
    """Return all customers from the database as dicts."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM customers WHERE active = 1 ORDER BY retailer_id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def load_promotions(db_path: Path) -> list[dict]:
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
