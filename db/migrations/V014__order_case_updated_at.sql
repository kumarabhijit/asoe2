-- ADR-038 / issue #133 PO #17 — surface OrderCase `updated_at`.
--
-- Operators flagged Created / Updated as audit-critical for SLA
-- tracking. `opened_at` covered "Created"; "Updated" was missing.
-- This migration adds a TEXT `updated_at` column on `order_case`
-- with a default of the existing `opened_at` (every prior row was
-- last modified no earlier than open). The application layer
-- (api/store.py::CaseStore) bumps the column on every mutation
-- (status flip, pending_override write, correlation-key append).
--
-- Forward-only DDL; rollback is a separate DROP migration if ever
-- needed (project policy).

ALTER TABLE order_case
    ADD COLUMN updated_at TEXT;

-- Backfill: every existing case was last modified no earlier than
-- it was opened. SQLite ALTER TABLE ADD COLUMN cannot reference
-- another column in the DEFAULT clause, so the seed runs as a
-- one-shot UPDATE keyed on the freshly-added NULL.
UPDATE order_case
   SET updated_at = opened_at
 WHERE updated_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_order_case_updated
    ON order_case (updated_at);
