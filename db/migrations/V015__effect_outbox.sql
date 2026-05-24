-- V015 — effect_outbox durable ledger (DoR #6).
--
-- Persists every gateway-effect outcome applied by the orchestration
-- `apply_effects` node: a SUCCESS external write is committed; a failure is
-- queued for compensation (needs_compensation) until a reconciler retries it to
-- success or escalates it. Survives process restarts so partial failures
-- (ERP-submit-OK / reply-fail) are reconcilable. Mirrors the in-memory ledger
-- in orchestration/outbox.py.

CREATE TABLE IF NOT EXISTS effect_outbox (
    id                 TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    trace_id           TEXT,
    recipe             TEXT,
    gateway            TEXT NOT NULL,
    operation          TEXT NOT NULL,
    status             TEXT NOT NULL,
    recipe_status      TEXT,
    committed          BOOLEAN NOT NULL DEFAULT FALSE,
    needs_compensation BOOLEAN NOT NULL DEFAULT FALSE,
    error              TEXT,
    params             JSONB NOT NULL DEFAULT '{}'::jsonb,
    attempts           INTEGER NOT NULL DEFAULT 0,
    escalated          BOOLEAN NOT NULL DEFAULT FALSE,
    compensated        BOOLEAN NOT NULL DEFAULT FALSE,
    compensated_at     TEXT,
    created_at         TEXT NOT NULL
);

-- The reconciler's work-queue lookup (pending = needs_compensation AND NOT
-- compensated AND NOT escalated), scoped per tenant.
CREATE INDEX IF NOT EXISTS idx_effect_outbox_pending
    ON effect_outbox (tenant_id, needs_compensation, compensated, escalated);

CREATE INDEX IF NOT EXISTS idx_effect_outbox_tenant_time
    ON effect_outbox (tenant_id, created_at);
