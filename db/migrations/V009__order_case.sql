-- ADR-038 Phase H.2 — OrderCase parent entity.
--
-- Introduces the case parent record. Per ADR-038 §6.1 the case carries
-- the source classification (manual_order / automated_order), the SLA
-- clock, and the lifecycle status of the business order it represents.
-- ExceptionRecord rows attach to a case via parent_case_id (V009 also
-- adds that nullable column on exceptions).
--
-- Forward-only DDL (no destructive ALTERs); rollback is a separate
-- DROP migration not bundled here (per project policy: rollbacks are
-- their own migration files when ever needed).

-- 1. order_case ----------------------------------------------------

CREATE TABLE IF NOT EXISTS order_case (
    case_id                  TEXT PRIMARY KEY,
    tenant_id                TEXT NOT NULL,
    customer_id              TEXT,
    source                   TEXT NOT NULL,
        -- Constrained to {manual_order, automated_order} by application
        -- layer (Pydantic Literal); deferred at DB level to avoid the
        -- enum-CHECK-constraint pattern V005 retired (intent enum).
    source_channel           TEXT NOT NULL,
    customer_po_number       TEXT,
    sales_order_id           TEXT,
    edi_transaction_id       TEXT,
    source_email_id          TEXT,
    opened_at                TEXT NOT NULL,
    closed_at                TEXT,
    status                   TEXT NOT NULL DEFAULT 'OPEN_AGENT_PROCESSING',
    sla_deadline             TEXT,
    tier                     INTEGER NOT NULL DEFAULT 2,
    working_memory_summary   TEXT,
    last_compaction_at       TEXT,
    bundle_version_at_open   TEXT
);

CREATE INDEX IF NOT EXISTS idx_order_case_tenant
    ON order_case (tenant_id);
CREATE INDEX IF NOT EXISTS idx_order_case_status
    ON order_case (status);
CREATE INDEX IF NOT EXISTS idx_order_case_sla
    ON order_case (sla_deadline);

-- 2. exceptions gains parent_case_id ------------------------------
--
-- Nullable: Tier-1 stateless records (clean Automated events that
-- auto-resolve to COMPLETE) never materialise a case. Per ADR-038
-- §7.2, lazy materialisation runs only on non-clean terminal states
-- and Manual Orders.
--
-- Table is `exceptions` (V001). The original V009/V011 referenced a
-- non-existent `exception_record` table, which aborts the Postgres
-- chain at this step (masked because CI only exercised the SQLite
-- mirror, which used the correct name — see V018 note). Corrected to
-- `exceptions` so the DB-backed (Azure) path actually applies;
-- prerequisite for Phase H.7 durable cases.

ALTER TABLE exceptions
    ADD COLUMN IF NOT EXISTS parent_case_id TEXT REFERENCES order_case(case_id);

CREATE INDEX IF NOT EXISTS idx_exception_parent_case
    ON exceptions (parent_case_id);
