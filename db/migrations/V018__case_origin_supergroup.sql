-- V018 — Case origin + supergroup_code + leaf intent_code on cases & children.
--
-- Phase 2 (step 1 of N) of docs/plans/case-intent-supergroup-implementation-plan.md.
-- Authority: docs/specs/case-intent-supergroup-requirements.md §5–§8.
--
-- Scope of THIS migration: schema + triggers + app_config seed. Backfill of
-- legacy data and the NOT NULL transitions land in a later step so this
-- migration is self-contained and reversible in isolation.
--
-- Inheritance behaviour (requirements §8.1):
--   v1 default = STRICT on both origins. The trigger reads
--   app_config.inheritance_mode_customer so the Steward can later flip
--   CUSTOMER to RELAXED via the §9.1 governance workflow with NO code
--   deploy and NO schema migration.
--
-- Leaf validity (criterion #4):
--   (supergroup_code, intent_code) must exist in supergroup_intent_allowed
--   and be effective on the current date.
--
-- NOTE: V009 and V011 incorrectly reference the table name `exception_record`
-- — the table is `exceptions` (V001). The Postgres migration set is
-- never exercised in CI (SQLite only), so this pre-existing bug has not
-- surfaced. V018 deliberately uses `exceptions` to stay correct on both
-- backends; out of scope to fix V009/V011 here.

-- ---------------------------------------------------------------------------
-- 1. app_config (operational levers, written by Steward via §9.1)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_config (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    description     TEXT,
    updated_at      TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
);

INSERT INTO app_config (key, value, description)
VALUES (
    'inheritance_mode_customer', 'STRICT',
    'v1 default. Steward may set to RELAXED per requirements §8.1 with no code deploy.'
)
ON CONFLICT (key) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 2. order_case columns
-- ---------------------------------------------------------------------------
ALTER TABLE order_case ADD COLUMN origin              TEXT;
ALTER TABLE order_case ADD COLUMN supergroup_code     TEXT REFERENCES case_supergroup(code);
ALTER TABLE order_case ADD COLUMN predecessor_case_id TEXT REFERENCES order_case(case_id);
ALTER TABLE order_case ADD COLUMN will_miss_rdd       BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE order_case ADD COLUMN sla_due_at          TEXT;

ALTER TABLE order_case
    ADD CONSTRAINT order_case_origin_check
    CHECK (origin IS NULL OR origin IN ('CUSTOMER','API'));

-- ---------------------------------------------------------------------------
-- 3. exceptions (a.k.a. child case) columns
-- ---------------------------------------------------------------------------
ALTER TABLE exceptions ADD COLUMN supergroup_code   TEXT REFERENCES case_supergroup(code);
ALTER TABLE exceptions ADD COLUMN divergence_reason TEXT;
ALTER TABLE exceptions ADD COLUMN intent_code       TEXT REFERENCES case_intent(code);
ALTER TABLE exceptions ADD COLUMN sap_block_field   TEXT;
ALTER TABLE exceptions ADD COLUMN scope             TEXT;

ALTER TABLE exceptions
    ADD CONSTRAINT exceptions_sap_block_field_check
    CHECK (
        sap_block_field IS NULL OR
        sap_block_field IN ('LIFSK','LIFSP','FAKSK','FAKSP','ABGRU','CMGST','Z_CUSTOM')
    );

ALTER TABLE exceptions
    ADD CONSTRAINT exceptions_scope_check
    CHECK (scope IS NULL OR scope IN ('HEADER','ITEM'));

-- ---------------------------------------------------------------------------
-- 4. Inheritance trigger (requirements §8.1)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION enforce_child_supergroup_inheritance()
RETURNS trigger AS $$
DECLARE
    parent_sg     TEXT;
    parent_origin TEXT;
    mode          TEXT;
BEGIN
    -- No parent or child supergroup not yet set: defer to other guards.
    IF NEW.parent_case_id IS NULL OR NEW.supergroup_code IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT supergroup_code, origin INTO parent_sg, parent_origin
    FROM order_case WHERE case_id = NEW.parent_case_id;

    IF parent_sg IS NULL THEN
        -- Parent has no supergroup yet (transitional). Allow.
        RETURN NEW;
    END IF;

    IF NEW.supergroup_code = parent_sg THEN
        RETURN NEW;
    END IF;

    -- Divergence detected.
    IF parent_origin = 'API' THEN
        RAISE EXCEPTION
          'API child cases cannot diverge from parent supergroup (child=%, parent=%)',
          NEW.supergroup_code, parent_sg;
    END IF;

    -- CUSTOMER origin: STRICT default; RELAXED requires divergence_reason.
    SELECT value INTO mode FROM app_config
        WHERE key = 'inheritance_mode_customer';
    IF mode IS NULL OR mode = 'STRICT' THEN
        RAISE EXCEPTION
          'CUSTOMER child cases cannot diverge under STRICT inheritance (child=%, parent=%)',
          NEW.supergroup_code, parent_sg;
    END IF;

    IF NEW.divergence_reason IS NULL THEN
        RAISE EXCEPTION
          'divergence_reason required under RELAXED mode when supergroup differs';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tg_child_supergroup_inheritance_ins ON exceptions;
CREATE TRIGGER tg_child_supergroup_inheritance_ins
    BEFORE INSERT ON exceptions
    FOR EACH ROW EXECUTE FUNCTION enforce_child_supergroup_inheritance();

DROP TRIGGER IF EXISTS tg_child_supergroup_inheritance_upd ON exceptions;
CREATE TRIGGER tg_child_supergroup_inheritance_upd
    BEFORE UPDATE OF supergroup_code ON exceptions
    FOR EACH ROW EXECUTE FUNCTION enforce_child_supergroup_inheritance();

-- ---------------------------------------------------------------------------
-- 5. Leaf validity trigger (criterion #4)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION enforce_child_leaf_validity()
RETURNS trigger AS $$
BEGIN
    IF NEW.intent_code IS NULL OR NEW.supergroup_code IS NULL THEN
        RETURN NEW;
    END IF;

    PERFORM 1 FROM supergroup_intent_allowed
    WHERE supergroup_code = NEW.supergroup_code
      AND intent_code = NEW.intent_code
      AND (deprecated_at IS NULL OR deprecated_at > CURRENT_DATE);

    IF NOT FOUND THEN
        RAISE EXCEPTION
          'leaf (intent_code=%) is not allowed under supergroup (%) per supergroup_intent_allowed',
          NEW.intent_code, NEW.supergroup_code;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tg_child_leaf_validity_ins ON exceptions;
CREATE TRIGGER tg_child_leaf_validity_ins
    BEFORE INSERT ON exceptions
    FOR EACH ROW EXECUTE FUNCTION enforce_child_leaf_validity();

DROP TRIGGER IF EXISTS tg_child_leaf_validity_upd ON exceptions;
CREATE TRIGGER tg_child_leaf_validity_upd
    BEFORE UPDATE OF intent_code, supergroup_code ON exceptions
    FOR EACH ROW EXECUTE FUNCTION enforce_child_leaf_validity();

-- ---------------------------------------------------------------------------
-- 6. Indexes for the new lookup columns
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_order_case_supergroup
    ON order_case (supergroup_code);
CREATE INDEX IF NOT EXISTS idx_order_case_origin
    ON order_case (origin);
CREATE INDEX IF NOT EXISTS idx_exceptions_intent_code
    ON exceptions (intent_code);
CREATE INDEX IF NOT EXISTS idx_exceptions_supergroup
    ON exceptions (supergroup_code);
