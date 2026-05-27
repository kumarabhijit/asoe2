-- V019 — Drop deprecated columns from order_case.
--
-- Authority: docs/specs/case-intent-supergroup-requirements.md §10.
-- The "one-release overlap" deprecation window in the original plan is
-- void — the system is not in production, so back-compat shims are
-- dead weight. Drops happen now.
--
-- Columns removed:
--   * order_case.source              -- replaced by origin (V018)
--   * order_case.case_type           -- replaced by supergroup_code (V018)
--   * order_case.email_classification -- replaced by supergroup_code (V018)
--
-- Pre-conditions:
--   * V018 ran (origin / supergroup_code columns exist).
--   * No application code reads these columns (verified by commit 6's
--     contracts/models.py + api/store.py refactor + test sweep).

ALTER TABLE order_case DROP COLUMN IF EXISTS source;
ALTER TABLE order_case DROP COLUMN IF EXISTS case_type;
ALTER TABLE order_case DROP COLUMN IF EXISTS email_classification;
