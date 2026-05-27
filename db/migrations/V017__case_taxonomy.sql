-- V017 — Case & Intent Super-Group taxonomy lookup tables.
--
-- Phase 1 of the implementation plan in
-- docs/plans/case-intent-supergroup-implementation-plan.md.
-- Authority: docs/specs/case-intent-supergroup-requirements.md §6, §7.
--
-- Schema only. Seed rows are inserted by ``scripts/seed_taxonomy.py``,
-- which the Postgres path of the runner invokes after applying this DDL
-- (see ``db/migrations/runner.py::apply_postgres``).
--
-- D1: this file is the schema; the YAML at ``db/seeds/case_taxonomy.yaml``
-- is the steward-owned source of truth for the row contents.
-- D7 (plan): no application code consumes these tables yet — Phase 2 adds
-- the FKs from order_case / exceptions into case_supergroup / case_intent.

CREATE TABLE IF NOT EXISTS case_supergroup (
    code              TEXT PRIMARY KEY,
    origin            TEXT NOT NULL,
    description       TEXT NOT NULL,
    owner_role        TEXT NOT NULL,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    effective_from    DATE NOT NULL,
    deprecated_at     DATE,
    replaced_by_code  TEXT REFERENCES case_supergroup(code),
    sort_order        INT NOT NULL DEFAULT 0,
    version           INT NOT NULL DEFAULT 1,
    CONSTRAINT case_supergroup_origin_check CHECK (origin IN ('CUSTOMER','API','BOTH'))
);

CREATE INDEX IF NOT EXISTS idx_case_supergroup_origin_active
    ON case_supergroup (origin, is_active);

CREATE TABLE IF NOT EXISTS case_intent (
    code              TEXT PRIMARY KEY,
    supergroup_code   TEXT NOT NULL REFERENCES case_supergroup(code),
    description       TEXT NOT NULL,
    sap_block_code    TEXT,
    sap_block_field   TEXT,
    sap_sales_org     TEXT,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    effective_from    DATE NOT NULL,
    deprecated_at     DATE,
    replaced_by_code  TEXT REFERENCES case_intent(code),
    CONSTRAINT case_intent_sap_field_check CHECK (
        sap_block_field IS NULL OR
        sap_block_field IN ('LIFSK','LIFSP','FAKSK','FAKSP','ABGRU','CMGST','Z_CUSTOM')
    )
);

CREATE INDEX IF NOT EXISTS idx_case_intent_supergroup
    ON case_intent (supergroup_code, is_active);

-- Uniqueness on (sap_block_code, sap_sales_org) where both non-null and
-- the row is active — a single SAP block code maps to one intent per
-- sales org. Sparse partial index keeps the constraint scoped.
CREATE UNIQUE INDEX IF NOT EXISTS uq_case_intent_sap_block_active
    ON case_intent (sap_block_code, COALESCE(sap_sales_org, ''))
    WHERE sap_block_code IS NOT NULL AND is_active;

CREATE TABLE IF NOT EXISTS supergroup_intent_allowed (
    supergroup_code   TEXT NOT NULL REFERENCES case_supergroup(code),
    intent_code       TEXT NOT NULL REFERENCES case_intent(code),
    effective_from    DATE NOT NULL,
    deprecated_at     DATE,
    PRIMARY KEY (supergroup_code, intent_code)
);

CREATE TABLE IF NOT EXISTS intent_label (
    code              TEXT NOT NULL,
    domain            TEXT NOT NULL,
    locale            TEXT NOT NULL DEFAULT 'en',
    display_name      TEXT NOT NULL,
    description       TEXT,
    PRIMARY KEY (code, domain, locale),
    CONSTRAINT intent_label_domain_check CHECK (domain IN ('SUPERGROUP','INTENT'))
);

CREATE INDEX IF NOT EXISTS idx_intent_label_locale
    ON intent_label (locale, domain);
