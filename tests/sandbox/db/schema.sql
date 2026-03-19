-- ASOE Sandbox — SQLite reference schema
-- This file is the authoritative schema definition.
-- seed.py uses this inline (via CREATE TABLE IF NOT EXISTS) to stay self-contained.

CREATE TABLE IF NOT EXISTS sap_pricing (
    sku          TEXT PRIMARY KEY,
    description  TEXT,
    base_price   REAL    NOT NULL,
    currency     TEXT    NOT NULL DEFAULT 'USD',
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

-- Credit exposure data used by CreditHoldReleaseRecipe.
-- current_exposure > credit_limit triggers a credit block event.
CREATE TABLE IF NOT EXISTS credit_profiles (
    retailer_id       TEXT PRIMARY KEY,
    credit_limit      REAL NOT NULL,
    current_exposure  REAL NOT NULL,
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
    metadata    TEXT    NOT NULL DEFAULT '{}',
    created_at  TEXT    NOT NULL
);
