-- V006 — tenant_config table for ADR-030 5-level hierarchy storage.
--
-- Stores layers 2-5 (tenant / tier / customer / channel) of the
-- DUPLICATE_PO score-weight hierarchy. Layer 1 (platform) stays on
-- disk as docs/specs/duplicate-po/config-defaults.json — that file
-- ships with the application and is never tenant-mutable. The
-- hash-chained edit history flows through the existing
-- policy_audit_log table via PolicyRepository.create_audit_event,
-- so this migration adds no audit-log columns.
--
-- Schema notes:
--   * `layer` is a free VARCHAR(20) with a CHECK against
--     ('tenant','tier','customer','channel'). The Python layer
--     (contracts.config_events.ConfigChangeEvent's discriminated
--     union) is the canonical source of valid layer values; the
--     CHECK is defence in depth.
--   * `scope_hash` is a SHA-256 hex digest of the canonical JSON
--     form of `scope`. Used as part of the unique key so the
--     application can do a single equality lookup without doing
--     JSONB containment matching (which has no obvious unique-
--     index analogue when the JSONB shape varies by layer).
--   * One active row per (tenant_id, layer, scope_hash). Edit
--     history lives on policy_audit_log, not in tenant_config.
--   * RLS mirrors policy_overrides — the per-row policy filters by
--     `app.current_tenant_id`.
--
-- Idempotent: CREATE TABLE / CREATE INDEX / DROP+CREATE POLICY all
-- tolerate re-runs.

CREATE TABLE IF NOT EXISTS tenant_config (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       VARCHAR(100) NOT NULL,
    layer           VARCHAR(20) NOT NULL,
    scope_hash      VARCHAR(64) NOT NULL,
    scope           JSONB NOT NULL DEFAULT '{}'::jsonb,
    weights         JSONB NOT NULL,
    created_by      VARCHAR(100) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_tenant_config_layer CHECK (
        layer IN ('tenant','tier','customer','channel')
    ),
    UNIQUE (tenant_id, layer, scope_hash)
);

CREATE INDEX IF NOT EXISTS idx_tenant_config_lookup
    ON tenant_config (tenant_id, layer);

ALTER TABLE tenant_config ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_tenant_config ON tenant_config;
CREATE POLICY tenant_isolation_tenant_config ON tenant_config
    USING (
        tenant_id = current_setting('app.current_tenant_id', true)
        AND current_setting('app.current_tenant_id', true) IS NOT NULL
    );
