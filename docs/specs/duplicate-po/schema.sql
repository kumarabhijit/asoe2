-- ============================================================================
-- B2B Duplicate PO Check — PostgreSQL Schema (REFERENCE — NOT IMPLEMENTED)
-- ============================================================================
--
-- Status: REFERENCE ONLY — preserved verbatim from the original product spec.
--
-- Per ADR-028 (Duplicate-PO Storage Shape), ASOE V1 maps these logical
-- entities onto the existing unified exception lifecycle:
--
--   incoming_po              → OrderEvent + OrderEvent.metadata
--   incoming_po_lines        → OrderEvent.metadata.lines
--   duplicate_check_results  → ExecutionLog.recipe_output
--   duplicate_check_audit    → audit_hash_chain (ADR-023)
--
-- The schema below is NOT applied as a migration in V1. It is preserved as
-- the original spec's intended shape for two reasons:
--   1. Future read-projection work (per ADR-031) may materialize a view
--      with this approximate shape.
--   2. Auditors and integration partners reading the original spec can find
--      the source-of-truth mapping in ADR-028.
--
-- Read this file alongside ADR-028 §"Decision" → mapping table.
-- ============================================================================

-- Incoming PO record (staging)
CREATE TABLE incoming_po (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(id),
    po_number         VARCHAR(50) NOT NULL,
    po_number_norm    VARCHAR(50) NOT NULL,  -- normalized for matching
    customer_id       UUID NOT NULL REFERENCES customers(id),
    submission_channel VARCHAR(20) NOT NULL,  -- 'EDI', 'API', 'EMAIL', 'PORTAL'
    submitted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total_amount      NUMERIC(12,2),
    currency          VARCHAR(3) DEFAULT 'USD',
    ship_to_address   JSONB,
    requested_delivery DATE,
    raw_payload       JSONB NOT NULL,        -- original EDI/JSON/XML
    status            VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    -- PENDING | PROCESSING | PASSED | FLAGGED | BLOCKED | RESOLVED
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- PO line items (staging)
CREATE TABLE incoming_po_lines (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incoming_po_id  UUID NOT NULL REFERENCES incoming_po(id),
    line_number     INT NOT NULL,
    sku             VARCHAR(50) NOT NULL,
    quantity        NUMERIC(10,2) NOT NULL,
    unit_price      NUMERIC(10,2),
    description     TEXT,
    UNIQUE(incoming_po_id, line_number)
);

-- Duplicate detection results
CREATE TABLE duplicate_check_results (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incoming_po_id    UUID NOT NULL REFERENCES incoming_po(id),
    matched_po_id     UUID,                  -- existing PO in OMS
    composite_score   NUMERIC(4,3) NOT NULL,  -- 0.000 to 1.000
    signal_breakdown  JSONB NOT NULL,         -- per-signal scores
    classification    VARCHAR(20) NOT NULL,
    -- AUTO_BLOCK | REVIEW_REQUIRED | SOFT_FLAG | PASS
    recommended_action VARCHAR(30),
    -- BLOCK_AND_NOTIFY | MERGE | SUPERSEDE | ALLOW_BOTH | ESCALATE | REQUEST_BUYER_CONFIRMATION
    agent_reasoning    TEXT,                  -- LLM-generated explanation
    autonomy_level     VARCHAR(5),           -- L1, L2, L3, L4
    resolved_by        UUID REFERENCES users(id),
    resolved_action    VARCHAR(30),          -- actual action taken
    resolved_at        TIMESTAMPTZ,
    resolution_notes   TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Audit log for compliance
CREATE TABLE duplicate_check_audit (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    check_result_id   UUID NOT NULL REFERENCES duplicate_check_results(id),
    action            VARCHAR(50) NOT NULL,
    actor_type        VARCHAR(10) NOT NULL,  -- 'AGENT' or 'HUMAN'
    actor_id          VARCHAR(100),
    details           JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- Performance Indexes
-- ============================================================================

-- Fast lookback queries
CREATE INDEX idx_incoming_po_customer_date
    ON incoming_po (customer_id, submitted_at DESC)
    WHERE status NOT IN ('BLOCKED', 'RESOLVED');

CREATE INDEX idx_incoming_po_norm
    ON incoming_po (po_number_norm, tenant_id);

-- Exception queue
CREATE INDEX idx_dup_check_pending
    ON duplicate_check_results (classification, created_at)
    WHERE resolved_at IS NULL;
