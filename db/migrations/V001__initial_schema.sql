-- V001 — Initial ASOE database schema
-- Implements architecture_v3.md Section 9.2
--
-- Tables: exceptions, traces, policy_overrides, policy_audit_log, checkpoints
-- Extensions: pgvector (installed, not indexed until V2)
--
-- This migration is PostgreSQL-native. SQLite-compatible subset is handled
-- by the migration runner (db/migrations/runner.py) for testing.

-- ── Extensions ──────────────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS "pgcrypto";     -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "vector";       -- pgvector (V2 readiness)

-- ── exceptions ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS exceptions (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         VARCHAR(100) NOT NULL,
    order_id          VARCHAR(100) NOT NULL,
    event_type        VARCHAR(50) NOT NULL,
    intent            VARCHAR(30) CHECK (intent IN (
                        'CONTRACTUAL_CORRECTION', 'CREDIT_BLOCK',
                        'MASS_PRICING_ERROR', 'DUPLICATE_PO', 'UNKNOWN')),
    lifecycle_state   VARCHAR(20) NOT NULL DEFAULT 'INGESTED',
    shadow_verdict    VARCHAR(10),
    selected_recipe   VARCHAR(50),
    final_status      VARCHAR(30),
    trace_id          UUID NOT NULL,
    resolution_data   JSONB DEFAULT '{}',
    resolved_by       VARCHAR(100),
    resolved_action   VARCHAR(30),
    resolution_notes  TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    context_embedding VECTOR(1536)  -- pgvector: installed, not indexed until V2
);

CREATE INDEX IF NOT EXISTS idx_exceptions_tenant_state
    ON exceptions (tenant_id, lifecycle_state, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_exceptions_trace
    ON exceptions (trace_id);
CREATE INDEX IF NOT EXISTS idx_exceptions_order
    ON exceptions (tenant_id, order_id);

-- ── traces ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS traces (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    exception_id    UUID NOT NULL REFERENCES exceptions(id),
    trace_id        UUID NOT NULL,
    tenant_id       VARCHAR(100) NOT NULL,
    trace_record    JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_traces_trace_id
    ON traces (trace_id);
CREATE INDEX IF NOT EXISTS idx_traces_tenant
    ON traces (tenant_id, created_at DESC);

-- ── policy_overrides ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS policy_overrides (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       VARCHAR(100) NOT NULL,
    policy_key      VARCHAR(100) NOT NULL,
    value           JSONB NOT NULL,
    effective_from  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    effective_until TIMESTAMPTZ,
    created_by      VARCHAR(100) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tenant_id, policy_key, effective_from)
);

-- ── policy_audit_log ────────────────────────────────────────────────────────
-- SOX compliance: immutable audit trail for policy changes.

CREATE TABLE IF NOT EXISTS policy_audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       VARCHAR(100) NOT NULL,
    policy_key      VARCHAR(100) NOT NULL,
    previous_value  JSONB,
    new_value       JSONB NOT NULL,
    changed_by      VARCHAR(100) NOT NULL,
    change_reason   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_policy_audit_tenant
    ON policy_audit_log (tenant_id, created_at DESC);

-- SOX immutability trigger: prevent UPDATE and DELETE on audit log
CREATE OR REPLACE FUNCTION prevent_audit_log_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'policy_audit_log is immutable — UPDATE and DELETE are prohibited (SOX requirement)';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_policy_audit_immutable
    BEFORE UPDATE OR DELETE ON policy_audit_log
    FOR EACH ROW EXECUTE FUNCTION prevent_audit_log_mutation();

-- ── checkpoints (V1.1 — HITL pause/resume) ─────────────────────────────────

CREATE TABLE IF NOT EXISTS checkpoints (
    trace_id        UUID PRIMARY KEY,
    tenant_id       VARCHAR(100) NOT NULL,
    graph_state     JSONB NOT NULL,
    interrupted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resumed_at      TIMESTAMPTZ,
    resumed_by      VARCHAR(100),
    status          VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    CONSTRAINT fk_checkpoint_exception FOREIGN KEY (trace_id)
        REFERENCES exceptions(trace_id)
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_pending
    ON checkpoints (status, interrupted_at)
    WHERE status = 'PENDING';

-- ── Row-Level Security (architecture_v3.md Section 11.3) ────────────────────

ALTER TABLE exceptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE traces ENABLE ROW LEVEL SECURITY;
ALTER TABLE policy_overrides ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkpoints ENABLE ROW LEVEL SECURITY;

-- Tenant isolation policy: returns rows only when app.current_tenant_id is set
-- and matches the row's tenant_id. Missing setting → zero rows (not all rows).
CREATE POLICY tenant_isolation_exceptions ON exceptions
    USING (
        tenant_id = current_setting('app.current_tenant_id', true)
        AND current_setting('app.current_tenant_id', true) IS NOT NULL
    );

CREATE POLICY tenant_isolation_traces ON traces
    USING (
        tenant_id = current_setting('app.current_tenant_id', true)
        AND current_setting('app.current_tenant_id', true) IS NOT NULL
    );

CREATE POLICY tenant_isolation_policy_overrides ON policy_overrides
    USING (
        tenant_id = current_setting('app.current_tenant_id', true)
        AND current_setting('app.current_tenant_id', true) IS NOT NULL
    );

CREATE POLICY tenant_isolation_checkpoints ON checkpoints
    USING (
        tenant_id = current_setting('app.current_tenant_id', true)
        AND current_setting('app.current_tenant_id', true) IS NOT NULL
    );

-- ── Schema version tracking ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     VARCHAR(50) PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
