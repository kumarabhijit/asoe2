-- Phase H.7 — durable cosign state on order_case.
--
-- The cosign / four-eyes flow (ADR-040) attaches a CasePendingOverride
-- to a case while it awaits a second approver. Until now that state
-- lived ONLY in the in-memory CaseStore (api/store.py), so it — like
-- the case itself — was lost on every container restart, silently
-- dropping in-flight four-eyes approvals. Phase H.7 makes OrderCase
-- DB-backed; this column gives the pending-override its durable home.
--
-- Stored as a single JSONB blob (mirrors enrichment_context / V004):
-- CasePendingOverride is read and written atomically as a unit and is
-- never queried by sub-field, so per-column flattening would add
-- schema surface with no query benefit. NULL = no override in flight.
--
-- Forward-only DDL (project policy); rollback is a separate DROP
-- migration if ever needed.

ALTER TABLE order_case
    ADD COLUMN IF NOT EXISTS pending_override JSONB;
