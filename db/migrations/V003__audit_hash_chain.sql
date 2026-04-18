-- V003 — Hash-chained, append-only policy_audit_log (Phase 4 #3)
--
-- Adds two columns + two triggers that turn the audit log into a
-- tamper-evident structure. Each row carries:
--   prev_hash   — hash of the predecessor row in the same tenant chain
--                 ("GENESIS" for the first row in the tenant)
--   event_hash  — sha256(prev_hash || canonical_event_json)
--
-- A reader can walk the rows by (tenant_id, created_at, id), recompute
-- each hash, and detect any edit/delete that left the chain incoherent.
-- The triggers below additionally enforce the append-only property at
-- the database level so a casual UPDATE/DELETE statement cannot mutate
-- the log without first dropping the trigger.
--
-- Backfill: existing rows are walked in (tenant_id, created_at, id) order
-- and assigned a real chain so the verifier reports valid from row 1
-- onward. Newly seeded databases have no rows, in which case the backfill
-- is a no-op.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + DROP/CREATE TRIGGER make this
-- safe to re-run.

-- Columns ---------------------------------------------------------------
ALTER TABLE policy_audit_log
    ADD COLUMN IF NOT EXISTS prev_hash TEXT NOT NULL DEFAULT 'GENESIS';

ALTER TABLE policy_audit_log
    ADD COLUMN IF NOT EXISTS event_hash TEXT NOT NULL DEFAULT '';

-- Backfill --------------------------------------------------------------
-- Postgres-side backfill uses a window-function CTE and digest() from
-- pgcrypto. Skipped if the chain is already populated (event_hash != '').
-- The runner.py module performs the equivalent walk for SQLite (which
-- has no built-in sha256).

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgcrypto') THEN
        NULL; -- pgcrypto already available
    ELSE
        BEGIN
            CREATE EXTENSION IF NOT EXISTS pgcrypto;
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE NOTICE 'pgcrypto unavailable; backfill skipped';
        END;
    END IF;
END $$;

WITH ordered AS (
    SELECT id, tenant_id, policy_key, previous_value, new_value,
           changed_by, change_reason, created_at,
           LAG(id) OVER (PARTITION BY tenant_id ORDER BY created_at, id) AS prev_id
      FROM policy_audit_log
     WHERE event_hash = ''
),
chained AS (
    SELECT id,
           COALESCE(
               (SELECT event_hash FROM policy_audit_log p
                  WHERE p.id = ordered.prev_id),
               'GENESIS'
           ) AS prev_hash_v
      FROM ordered
)
UPDATE policy_audit_log al
   SET prev_hash = chained.prev_hash_v,
       event_hash = encode(
           digest(
               chained.prev_hash_v || '|' ||
               jsonb_build_object(
                   'change_reason',  al.change_reason,
                   'changed_by',     al.changed_by,
                   'created_at',     al.created_at,
                   'id',             al.id,
                   'new_value',      al.new_value,
                   'policy_key',     al.policy_key,
                   'prev_hash',      chained.prev_hash_v,
                   'previous_value', al.previous_value,
                   'tenant_id',      al.tenant_id
               )::text,
               'sha256'
           ),
           'hex'
       )
  FROM chained
 WHERE al.id = chained.id;

-- Triggers --------------------------------------------------------------
-- Mutation rejection. A privileged operator who genuinely needs to
-- correct the log must DROP the triggers, log the operation, perform
-- the surgery, and rebuild the chain — leaving an obvious trail.

CREATE OR REPLACE FUNCTION reject_audit_log_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'policy_audit_log is append-only; % rejected (drop trigger to override)',
        TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS policy_audit_log_no_update ON policy_audit_log;
CREATE TRIGGER policy_audit_log_no_update
    BEFORE UPDATE ON policy_audit_log
    FOR EACH ROW
    EXECUTE FUNCTION reject_audit_log_mutation();

DROP TRIGGER IF EXISTS policy_audit_log_no_delete ON policy_audit_log;
CREATE TRIGGER policy_audit_log_no_delete
    BEFORE DELETE ON policy_audit_log
    FOR EACH ROW
    EXECUTE FUNCTION reject_audit_log_mutation();

-- Index to speed up per-tenant chain walks.
CREATE INDEX IF NOT EXISTS idx_policy_audit_chain
    ON policy_audit_log (tenant_id, created_at, id);
