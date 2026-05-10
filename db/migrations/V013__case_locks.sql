-- ADR-038 Phase H.5 — case_locks lightweight mutex table.
--
-- Cross-pod concurrency control for the L4 harness. The
-- in-process `agents.harness.CaseLockManager` is correct for
-- single-process / single-pod deployments; this table is the
-- equivalent for multi-pod deployments where two pods could
-- otherwise both run the agent against the same case.
--
-- Acquire: INSERT a row keyed on case_id; UNIQUE constraint
--   makes a concurrent acquire on the same case raise IntegrityError,
--   which the application catches and translates to "lock held".
-- Release: DELETE the row; the harness always releases in a
--   `finally` block.
-- Stale: agent runs are bounded by `agents.budget.CaseBudget`
--   (≤ 12 seconds wall-clock for T3); orphan rows are an
--   ops anomaly. `expires_at` is recorded so a janitor can
--   sweep them safely.
--
-- Forward-only DDL.

CREATE TABLE IF NOT EXISTS case_locks (
    case_id        TEXT PRIMARY KEY,
        -- The case_id under lock. References order_case(case_id) but
        -- not declared as a foreign key — locks acquire even on
        -- newly-opened cases the FK might not see in the same
        -- transaction.
    tenant_id      TEXT NOT NULL,
    acquired_at    TEXT NOT NULL,
        -- ISO-8601 UTC.
    acquired_by    TEXT,
        -- Free-form holder identifier (pod / process id / hostname).
        -- Diagnostic only; the application never branches on it.
    expires_at     TEXT NOT NULL
        -- ISO-8601 UTC. Hard upper bound for janitor sweeps;
        -- rows older than this are stale.
);

-- Janitor query: tenant-scoped sweep of stale locks.
CREATE INDEX IF NOT EXISTS idx_case_locks_expires
    ON case_locks (expires_at);
