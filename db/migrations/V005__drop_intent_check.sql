-- V005__drop_intent_check.sql
--
-- Drops the CHECK constraint that V001__initial_schema.sql put on
-- ``exceptions.intent``. The constraint enumerated only 5 of the 11
-- intents the system actually classifies — every BACK_ORDER /
-- DELIVERY_DELAY / EDI_MISMATCH / OVER_MAX / MIN_ORDER_QTY /
-- PALLET_CONFIG / PRICE_HOLD_RELEASE write hit
-- ``CheckViolation: exceptions_intent_check`` and the recipe
-- pipeline returned 500.
--
-- Per the design intent already documented in db/migrations/runner.py
-- (``a SQL CHECK constraint here drifts every time a new intent
-- ships``), the SQLite path never had this constraint. V005 brings
-- Postgres in line: the intent enum is enforced at the Python layer
-- via ``constraints.specs.AllowedIntent`` and exposed via
-- ``GET /api/v1/health.allowed_intents``. The DB column stays a free
-- VARCHAR(30); validation lives at the application boundary.
--
-- Idempotent: ALTER TABLE ... DROP CONSTRAINT IF EXISTS is a no-op
-- when the constraint is already absent.

ALTER TABLE exceptions
    DROP CONSTRAINT IF EXISTS exceptions_intent_check;
