-- V007 — DB-level metadata-contract enforcement for DUPLICATE_PO rows.
--
-- ADR-028 G1 / metadata-contract.md V1.5: complements the orchestration-
-- layer gate (build_analysis in api/analysis_composer.py, shipped in
-- PR-A / PR #96) with a DB-level enforcement of the same invariant.
-- A DUPLICATE_PO exception row that has reached a terminal state
-- (final_status IS NOT NULL) MUST carry the four resolution_data keys
-- the audit + envelope path depend on:
--
--   signal_breakdown      — per-signal contribution map
--   composite_score       — overall similarity score (0..1)
--   classification        — recipe-emitted bucket (AUTO_BLOCK | ...)
--   recommended_action    — agent-recommended action token
--
-- A row that violates this contract is rejected at INSERT/UPDATE time
-- with a check_violation. The trigger is intent-scoped and final-
-- status-scoped: rows that are still pre-recipe (final_status IS NULL)
-- and rows of other intents are unaffected.
--
-- Defense-in-depth rationale (V1.5): the orchestration-layer gate
-- catches violations before the row is written when the recipe runs
-- through build_analysis. Direct repository writes (HITL flows,
-- reanalysis, manual injection paths) bypass that gate — V1.5 closes
-- the loop so a malformed DUPLICATE_PO row simply cannot be persisted
-- regardless of the call site.
--
-- Idempotent: CREATE OR REPLACE FUNCTION + DROP+CREATE TRIGGER tolerate
-- re-runs.

CREATE OR REPLACE FUNCTION validate_duplicate_po_metadata_contract()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.intent = 'DUPLICATE_PO' AND NEW.final_status IS NOT NULL THEN
        IF NEW.resolution_data IS NULL
           OR NOT (NEW.resolution_data ? 'signal_breakdown') THEN
            RAISE EXCEPTION
                'metadata-contract violation: DUPLICATE_PO row missing resolution_data.signal_breakdown (id=%)',
                NEW.id
                USING ERRCODE = 'check_violation';
        END IF;
        IF NOT (NEW.resolution_data ? 'composite_score') THEN
            RAISE EXCEPTION
                'metadata-contract violation: DUPLICATE_PO row missing resolution_data.composite_score (id=%)',
                NEW.id
                USING ERRCODE = 'check_violation';
        END IF;
        IF NOT (NEW.resolution_data ? 'classification') THEN
            RAISE EXCEPTION
                'metadata-contract violation: DUPLICATE_PO row missing resolution_data.classification (id=%)',
                NEW.id
                USING ERRCODE = 'check_violation';
        END IF;
        IF NOT (NEW.resolution_data ? 'recommended_action') THEN
            RAISE EXCEPTION
                'metadata-contract violation: DUPLICATE_PO row missing resolution_data.recommended_action (id=%)',
                NEW.id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS exceptions_duplicate_po_metadata_contract
    ON exceptions;
CREATE TRIGGER exceptions_duplicate_po_metadata_contract
    BEFORE INSERT OR UPDATE ON exceptions
    FOR EACH ROW
    EXECUTE FUNCTION validate_duplicate_po_metadata_contract();
