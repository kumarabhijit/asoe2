-- ADR-038 Phase H.2 — case correlation table.
--
-- Per ADR-038 §6.2: when an inbound event arrives, the harness
-- extracts whichever correlation identifiers are present
-- (customer_po, sales_order_id, edi_transaction_id, source_email_id)
-- and looks them up here. First match wins; new keys join the case.
--
-- Resolution priority (enforced in the application layer, not the DB):
--   sales_order_id → customer_po_number → edi_transaction_id → source_email_id
--
-- Multi-PO email opens N cases (one per customer PO). The PK is
-- (tenant_id, key_type, key_value) so the same key value cannot
-- ambiguously map to two cases within a tenant.

CREATE TABLE IF NOT EXISTS case_correlation_keys (
    tenant_id      TEXT NOT NULL,
    key_type       TEXT NOT NULL,
        -- Constrained to one of:
        --   'customer_po_number' | 'sales_order_id'
        --   | 'edi_transaction_id' | 'source_email_id'
        -- Application-layer Literal enforces the vocabulary; not
        -- pinned at DB level (consistent with V005 enum-decoupling).
    key_value      TEXT NOT NULL,
    case_id        TEXT NOT NULL REFERENCES order_case(case_id),
    registered_at  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, key_type, key_value)
);

CREATE INDEX IF NOT EXISTS idx_case_corr_case_id
    ON case_correlation_keys (case_id);
