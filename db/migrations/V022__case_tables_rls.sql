-- V022 — Row-Level Security for the case tables (Phase H.7 hardening).
--
-- V001 enabled RLS + tenant_isolation policies on exceptions / traces /
-- policy_overrides / checkpoints (architecture_v3.md §11.3). The case
-- tables added later — order_case (V009), case_correlation_keys (V010),
-- case_classification_history (V020) — never got the same treatment.
-- Once OrderCase became DB-backed (Phase H.7) those tables hold the same
-- tenant-confidential, SOX-relevant data as exceptions, so they must
-- carry the identical defense-in-depth posture.
--
-- This mirrors V001 exactly: ENABLE (not FORCE) + a tenant_isolation
-- policy keyed on the app.current_tenant_id session var. Like V001 it is
-- NOT forced, so the table-owner app role bypasses it (app-layer
-- `WHERE tenant_id = ?` remains the primary isolation); the policy
-- enforces if a non-owner role ever connects. Missing session setting →
-- zero rows (fail-closed), never all rows.
--
-- DROP POLICY IF EXISTS guards make re-application safe (Postgres has no
-- CREATE POLICY IF NOT EXISTS). ENABLE ROW LEVEL SECURITY is idempotent.

ALTER TABLE order_case ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_correlation_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_classification_history ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_order_case ON order_case;
CREATE POLICY tenant_isolation_order_case ON order_case
    USING (
        tenant_id = current_setting('app.current_tenant_id', true)
        AND current_setting('app.current_tenant_id', true) IS NOT NULL
    );

DROP POLICY IF EXISTS tenant_isolation_case_correlation_keys ON case_correlation_keys;
CREATE POLICY tenant_isolation_case_correlation_keys ON case_correlation_keys
    USING (
        tenant_id = current_setting('app.current_tenant_id', true)
        AND current_setting('app.current_tenant_id', true) IS NOT NULL
    );

DROP POLICY IF EXISTS tenant_isolation_case_classification_history ON case_classification_history;
CREATE POLICY tenant_isolation_case_classification_history ON case_classification_history
    USING (
        tenant_id = current_setting('app.current_tenant_id', true)
        AND current_setting('app.current_tenant_id', true) IS NOT NULL
    );
