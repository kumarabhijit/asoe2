-- V020 — case_classification_history (append-only audit trail).
--
-- Authority: docs/specs/case-intent-supergroup-requirements.md §8.6.
-- Plan: docs/plans/case-intent-supergroup-implementation-plan.md §5.1
-- (Phase 3 — Classification audit).
--
-- Every classification or reclassification event (initial classify, CSR
-- retag, lead reclassify, model reclassify, steward correction) writes
-- exactly one row here. Reads (the case-level UI history strip + ops
-- reports) join back to ``order_case`` / ``exceptions`` via case_id and
-- child_case_id.
--
-- Append-only enforcement: the app role gets INSERT-only on this table.
-- A trigger blocks UPDATE/DELETE outright so a misconfigured DB role
-- cannot rewrite history. Follows the V003 policy_audit_log pattern.

CREATE TABLE IF NOT EXISTS case_classification_history (
    id                  TEXT PRIMARY KEY,
    case_id             TEXT NOT NULL REFERENCES order_case(case_id),
    -- NULL for parent-level (case) reclassifications; populated for any
    -- per-child reclassification.
    child_case_id       TEXT REFERENCES exceptions(id),
    supergroup_code     TEXT NOT NULL REFERENCES case_supergroup(code),
    -- NULL when this row records a parent-level supergroup change with
    -- no concurrent leaf classification.
    intent_code         TEXT REFERENCES case_intent(code),
    classified_at       TEXT NOT NULL,
    classified_by       TEXT NOT NULL,
    classifier_type     TEXT NOT NULL,
    model_version       TEXT,
    reason_text         TEXT,
    source_event_id     TEXT,
    -- Snapshot of the YAML taxonomy version in effect at the time, so a
    -- later mapping change does not retroactively rewrite "what did we
    -- classify under?" for historical reporting.
    taxonomy_version    TEXT NOT NULL,
    CONSTRAINT case_classification_history_classifier_type_check
        CHECK (classifier_type IN ('HUMAN','MODEL','RULE'))
);

CREATE INDEX IF NOT EXISTS idx_case_classification_history_case_time
    ON case_classification_history (case_id, classified_at);
CREATE INDEX IF NOT EXISTS idx_case_classification_history_child_time
    ON case_classification_history (child_case_id, classified_at)
    WHERE child_case_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_case_classification_history_classifier
    ON case_classification_history (classifier_type, classified_at);

-- Append-only enforcement (mirrors V003).
CREATE OR REPLACE FUNCTION case_classification_history_block_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
      'case_classification_history is append-only; % rejected',
      TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tg_cch_no_update ON case_classification_history;
CREATE TRIGGER tg_cch_no_update
    BEFORE UPDATE ON case_classification_history
    FOR EACH ROW EXECUTE FUNCTION case_classification_history_block_mutation();

DROP TRIGGER IF EXISTS tg_cch_no_delete ON case_classification_history;
CREATE TRIGGER tg_cch_no_delete
    BEFORE DELETE ON case_classification_history
    FOR EACH ROW EXECUTE FUNCTION case_classification_history_block_mutation();
