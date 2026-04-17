-- V002 — Promote reanalyze fields to dedicated columns
--
-- Prior to this migration, `original_event` and `reanalysis_history` were
-- stashed inside `resolution_data` under reserved keys to avoid a schema
-- change while the reanalyze feature was taking shape. They're now
-- first-class columns so they can be indexed, queried, and migrated
-- independently of recipe resolution output.
--
-- Idempotent: safe to re-run. `original_event` remains nullable
-- (pre-feature records have no source event on file).

ALTER TABLE exceptions
    ADD COLUMN IF NOT EXISTS original_event JSONB;

ALTER TABLE exceptions
    ADD COLUMN IF NOT EXISTS reanalysis_history JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Index to support "find exceptions with more than N reanalyses" queries,
-- which the audit dashboard will need for rate-limit visibility.
CREATE INDEX IF NOT EXISTS idx_exceptions_reanalysis_count
    ON exceptions (tenant_id, (jsonb_array_length(reanalysis_history)));
