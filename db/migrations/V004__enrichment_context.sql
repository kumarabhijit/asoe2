-- V004 — Persist enrichment_context as a first-class JSONB column
--
-- Verdict Pillar 1 (2026-04-22 compliance workshop): gateway-fetched
-- evidence (matched POs, warehouse snapshots, contract refs, SAP doc
-- numbers) must survive the request that produced it so a later audit
-- can replay the operator's decision context.
--
-- Pre-migration, `DbExceptionStore.create()` attached enrichment_context
-- to the returned record in-memory only — gateway evidence vanished on
-- process restart. This migration promotes the bag to a durable column
-- alongside `original_event` (V002) and retires the in-memory bridge.
--
-- Backfill: existing rows default to '{}' (empty bag). Re-resolving
-- those exceptions re-runs the recipe and re-populates the column.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS makes this safe to re-run.

ALTER TABLE exceptions
    ADD COLUMN IF NOT EXISTS enrichment_context JSONB NOT NULL DEFAULT '{}'::jsonb;
