"""ADR-028 G1 / action item A5 — DUPLICATE_PO metadata-contract tests.

Two layers:

  1. Pure unit tests on the validators in
     `contracts.duplicate_po_contract` — happy path + every named
     violation mode (key set, range, type, sum, etc.).

  2. Integration tests on the orchestration-tail enforcement: a
     `GraphState` with malformed event metadata or recipe output is
     run through `build_analysis` and asserted to land at
     `TerminalStatus.AUDIT_CONTEXT_MISSING` with an explanation that
     names the offending key.

The recipe-side weight contract has its own dedicated test file
(`test_duplicate_po_weights.py`) — this file covers the metadata-
contract surface specifically.
"""

from __future__ import annotations

import pytest

from contracts.duplicate_po_contract import (
    DuplicatePOEventMetadata,
    DuplicatePORecipeOutput,
    MetadataContractViolation,
    validate_duplicate_po_event_metadata,
    validate_duplicate_po_recipe_output,
)
from contracts.models import (
    ExecutionLog,
    GraphState,
    Intent,
    OrderEvent,
    TerminalStatus,
)
from orchestration.nodes import build_analysis


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _valid_signal_scores() -> dict:
    return {
        "po_number": 1.0, "customer_id": 1.0, "line_items": 0.95,
        "amount": 0.90, "timestamp": 0.80, "ship_to": 0.80,
        "channel": 1.0, "delivery_date": 0.80,
    }


def _valid_event_metadata() -> dict:
    return {
        "signal_scores": _valid_signal_scores(),
        "matched_po_id": "PO-DUP-PRIOR",
    }


def _valid_recipe_output() -> dict:
    return {
        "status": "BLOCKED",
        "composite_score": 0.93,
        "classification": "AUTO_BLOCK",
        "recommended_action": "BLOCK_AND_NOTIFY",
        "autonomy_level": "L3",
        "notification_template": "duplicate_po_blocked",
        "signal_breakdown": {
            "po_number": 0.30, "customer_id": 0.15, "line_items": 0.19,
            "amount": 0.09, "timestamp": 0.08, "ship_to": 0.04,
            "channel": 0.05, "delivery_date": 0.04,
        },
        "incoming_po_number": "PO-4001",
        "customer_id": "R-10",
    }


# ---------------------------------------------------------------------------
# Input contract — DuplicatePOEventMetadata
# ---------------------------------------------------------------------------


class TestDuplicatePOEventMetadata:
    def test_valid_metadata_parses(self):
        parsed = validate_duplicate_po_event_metadata(_valid_event_metadata())
        assert isinstance(parsed, DuplicatePOEventMetadata)
        assert parsed.matched_po_id == "PO-DUP-PRIOR"
        assert parsed.signal_scores["po_number"] == 1.0

    def test_optional_keys_default_to_none(self):
        parsed = validate_duplicate_po_event_metadata(_valid_event_metadata())
        assert parsed.tenant_id is None
        assert parsed.customer_tier is None
        assert parsed.channel is None
        assert parsed.behavior_tag is None

    def test_cross_cutting_keys_allowed(self):
        meta = _valid_event_metadata()
        # Tracing / debug / propagation keys flow through opaquely.
        meta["request_id"] = "req-abc"
        meta["debug_flag"] = True
        # Should not raise.
        validate_duplicate_po_event_metadata(meta)

    def test_full_optional_metadata_parses(self):
        meta = _valid_event_metadata()
        meta.update({
            "tenant_id": "acme",
            "customer_tier": "strategic",
            "channel": "EDI",
            "behavior_tag": "blanket_po",
        })
        parsed = validate_duplicate_po_event_metadata(meta)
        assert parsed.tenant_id == "acme"
        assert parsed.customer_tier == "strategic"
        assert parsed.behavior_tag == "blanket_po"

    # -- violations ----------------------------------------------------------

    def test_missing_signal_scores_raises(self):
        meta = {"matched_po_id": "PO-X"}
        with pytest.raises(MetadataContractViolation) as exc_info:
            validate_duplicate_po_event_metadata(meta)
        offender_keys = {k for k, _r in exc_info.value.offenders}
        assert "signal_scores" in offender_keys

    def test_missing_matched_po_id_raises(self):
        meta = {"signal_scores": _valid_signal_scores()}
        with pytest.raises(MetadataContractViolation) as exc_info:
            validate_duplicate_po_event_metadata(meta)
        offender_keys = {k for k, _r in exc_info.value.offenders}
        assert "matched_po_id" in offender_keys

    def test_empty_matched_po_id_raises(self):
        meta = _valid_event_metadata()
        meta["matched_po_id"] = ""  # min_length=1 violated
        with pytest.raises(MetadataContractViolation):
            validate_duplicate_po_event_metadata(meta)

    def test_unknown_signal_key_raises(self):
        meta = _valid_event_metadata()
        meta["signal_scores"]["bogus_signal"] = 0.5
        with pytest.raises(MetadataContractViolation, match="unknown signal keys"):
            validate_duplicate_po_event_metadata(meta)

    def test_signal_score_above_one_raises(self):
        meta = _valid_event_metadata()
        meta["signal_scores"]["po_number"] = 1.5
        with pytest.raises(MetadataContractViolation, match="outside"):
            validate_duplicate_po_event_metadata(meta)

    def test_signal_score_negative_raises(self):
        meta = _valid_event_metadata()
        meta["signal_scores"]["po_number"] = -0.1
        with pytest.raises(MetadataContractViolation, match="outside"):
            validate_duplicate_po_event_metadata(meta)

    def test_invalid_customer_tier_raises(self):
        meta = _valid_event_metadata()
        meta["customer_tier"] = "platinum"  # not in Literal
        with pytest.raises(MetadataContractViolation):
            validate_duplicate_po_event_metadata(meta)

    def test_invalid_behavior_tag_raises(self):
        meta = _valid_event_metadata()
        meta["behavior_tag"] = "made_up_tag"
        with pytest.raises(MetadataContractViolation):
            validate_duplicate_po_event_metadata(meta)


# ---------------------------------------------------------------------------
# Output contract — DuplicatePORecipeOutput
# ---------------------------------------------------------------------------


class TestDuplicatePORecipeOutput:
    def test_valid_output_parses(self):
        parsed = validate_duplicate_po_recipe_output(_valid_recipe_output())
        assert isinstance(parsed, DuplicatePORecipeOutput)
        assert parsed.status == "BLOCKED"
        assert parsed.recommended_action == "BLOCK_AND_NOTIFY"
        assert parsed.composite_score == 0.93

    def test_optional_autonomy_level_can_be_none(self):
        out = _valid_recipe_output()
        out["autonomy_level"] = None
        out["notification_template"] = None
        validate_duplicate_po_recipe_output(out)

    # -- violations ----------------------------------------------------------

    def test_extra_key_raises(self):
        out = _valid_recipe_output()
        out["bogus_key"] = "anything"
        with pytest.raises(MetadataContractViolation):
            validate_duplicate_po_recipe_output(out)

    def test_invalid_status_raises(self):
        out = _valid_recipe_output()
        out["status"] = "MAYBE"
        with pytest.raises(MetadataContractViolation):
            validate_duplicate_po_recipe_output(out)

    def test_invalid_recommended_action_raises(self):
        out = _valid_recipe_output()
        out["recommended_action"] = "DELETE_EVERYTHING"
        with pytest.raises(MetadataContractViolation):
            validate_duplicate_po_recipe_output(out)

    def test_composite_score_above_one_raises(self):
        out = _valid_recipe_output()
        out["composite_score"] = 1.5
        with pytest.raises(MetadataContractViolation):
            validate_duplicate_po_recipe_output(out)

    def test_composite_score_below_zero_raises(self):
        out = _valid_recipe_output()
        out["composite_score"] = -0.1
        with pytest.raises(MetadataContractViolation):
            validate_duplicate_po_recipe_output(out)

    def test_missing_signal_breakdown_key_raises(self):
        out = _valid_recipe_output()
        del out["signal_breakdown"]["delivery_date"]
        with pytest.raises(MetadataContractViolation, match="missing signal keys"):
            validate_duplicate_po_recipe_output(out)

    def test_extra_signal_breakdown_key_raises(self):
        out = _valid_recipe_output()
        out["signal_breakdown"]["bogus_signal"] = 0.0
        with pytest.raises(MetadataContractViolation, match="unknown signal keys"):
            validate_duplicate_po_recipe_output(out)

    def test_invalid_autonomy_level_raises(self):
        out = _valid_recipe_output()
        out["autonomy_level"] = "L9"
        with pytest.raises(MetadataContractViolation):
            validate_duplicate_po_recipe_output(out)


# ---------------------------------------------------------------------------
# MetadataContractViolation surface
# ---------------------------------------------------------------------------


class TestMetadataContractViolation:
    def test_carries_contract_name_and_offenders(self):
        try:
            validate_duplicate_po_event_metadata({"matched_po_id": "x"})
        except MetadataContractViolation as exc:
            assert "DUPLICATE_PO" in exc.contract_name
            assert exc.offenders, "offenders list must be non-empty on violation"
            for key, reason in exc.offenders:
                assert isinstance(key, str)
                assert isinstance(reason, str)
        else:
            pytest.fail("expected MetadataContractViolation")

    def test_str_summary_includes_offender_keys(self):
        try:
            validate_duplicate_po_event_metadata({"matched_po_id": "x"})
        except MetadataContractViolation as exc:
            assert "signal_scores" in str(exc)


# ---------------------------------------------------------------------------
# build_analysis integration — orchestration-tail enforcement
# ---------------------------------------------------------------------------


def _state_with_duplicate_po_intent(
    metadata: dict,
    recipe_outputs: dict | None,
) -> GraphState:
    """Build a minimal GraphState for build_analysis. Bypasses earlier
    nodes — we test the contract gate in isolation."""
    state = GraphState(
        event=OrderEvent(
            order_id="PO-DUP-X",
            line_item=1,
            po_price=100.0,
            sap_base_price=100.0,
            event_type="EDI_850_DUPLICATE_PO",
            retailer_id="R-10",
            line_count=1,
            metadata=metadata,
        ),
    )
    state.intent = Intent.DUPLICATE_PO
    state.selected_recipe = "DuplicatePORecipe.py"
    if recipe_outputs is not None:
        state.execution_log = ExecutionLog(
            trace_id="t-1",
            recipe_name="DuplicatePORecipe.py",
            outputs=recipe_outputs,
        )
        # Mirror the GREEN-success final_status the executor would have
        # set (so build_analysis doesn't short-circuit on FAIL_TO_HUMAN
        # or AUDIT_CONTEXT_MISSING).
        state.final_status = TerminalStatus.COMPLETE
    return state


class TestBuildAnalysisContractGate:
    def test_valid_event_and_output_pass_through(self):
        """Happy path — valid contract leaves final_status untouched
        for downstream registry-coverage check to evaluate."""
        state = _state_with_duplicate_po_intent(
            metadata=_valid_event_metadata(),
            recipe_outputs=_valid_recipe_output(),
        )
        out = build_analysis(state)
        # The contract gate didn't change final_status. Downstream
        # registry-coverage check may further mutate it (likely to
        # AUDIT_CONTEXT_MISSING because composer registry rows for
        # DuplicatePOAnalysisData aren't there yet — that's A6).
        # Assert specifically that the contract gate did NOT inject a
        # metadata-contract violation explanation.
        explanation = out.explanation or ""
        assert "Metadata-contract violation" not in explanation

    def test_invalid_event_metadata_routes_to_audit_context_missing(self):
        bad_metadata = {"matched_po_id": "PO-X"}  # missing signal_scores
        state = _state_with_duplicate_po_intent(
            metadata=bad_metadata,
            recipe_outputs=None,
        )
        out = build_analysis(state)
        assert out.final_status == TerminalStatus.AUDIT_CONTEXT_MISSING
        assert "Metadata-contract violation" in (out.explanation or "")
        assert "signal_scores" in (out.explanation or "")

    def test_invalid_recipe_output_routes_to_audit_context_missing(self):
        bad_outputs = _valid_recipe_output()
        bad_outputs["status"] = "MAYBE_BLOCKED"
        state = _state_with_duplicate_po_intent(
            metadata=_valid_event_metadata(),
            recipe_outputs=bad_outputs,
        )
        out = build_analysis(state)
        assert out.final_status == TerminalStatus.AUDIT_CONTEXT_MISSING
        explanation = out.explanation or ""
        assert "Metadata-contract violation" in explanation
        assert "ExecutionLog.outputs" in explanation
        assert "status" in explanation

    def test_explanation_lists_named_offender_keys(self):
        bad_metadata = _valid_event_metadata()
        bad_metadata["signal_scores"]["po_number"] = 1.5  # out of range
        bad_metadata["signal_scores"]["bogus_signal"] = 0.5  # extra key
        state = _state_with_duplicate_po_intent(
            metadata=bad_metadata,
            recipe_outputs=None,
        )
        out = build_analysis(state)
        explanation = out.explanation or ""
        # Both offender details must be on the explanation so the
        # auditor sees them without needing to drill into the trace.
        assert "po_number" in explanation
        assert "bogus_signal" in explanation

    def test_non_duplicate_po_intent_skips_contract_gate(self):
        """Records for other intents pass through the contract gate
        untouched — the gate is scoped to DUPLICATE_PO only."""
        state = GraphState(
            event=OrderEvent(
                order_id="SO-CC-1",
                line_item=1,
                po_price=90.0,
                sap_base_price=100.0,
                event_type="EDI_850_PRICE_MISMATCH",
                metadata={},  # would fail DUPLICATE_PO contract; should be ignored
            ),
        )
        state.intent = Intent.CONTRACTUAL_CORRECTION
        state.final_status = TerminalStatus.COMPLETE
        out = build_analysis(state)
        # No metadata-contract violation explanation — gate skipped.
        assert "Metadata-contract violation" not in (out.explanation or "")

    def test_fail_to_human_short_circuits_before_contract_gate(self):
        """build_analysis already short-circuits on FAIL_TO_HUMAN; the
        contract gate must respect that — a record halted earlier in
        the pipeline shouldn't be re-judged by the metadata contract."""
        state = _state_with_duplicate_po_intent(
            metadata={},  # would fail contract
            recipe_outputs=None,
        )
        state.final_status = TerminalStatus.FAIL_TO_HUMAN
        state.explanation = "circuit breaker tripped"
        out = build_analysis(state)
        # Should remain FAIL_TO_HUMAN — contract gate is not entered.
        assert out.final_status == TerminalStatus.FAIL_TO_HUMAN
        assert "Metadata-contract violation" not in (out.explanation or "")

    def test_no_recipe_output_skips_output_check(self):
        """When the recipe didn't execute (execution_log empty), only
        the input-side contract is checked — not the output-side."""
        state = _state_with_duplicate_po_intent(
            metadata=_valid_event_metadata(),
            recipe_outputs=None,
        )
        out = build_analysis(state)
        # Input was valid; output was absent (legitimately). Contract
        # gate should not flag this as a violation. Whether the
        # downstream registry-coverage check flags it is a separate
        # concern.
        assert "Metadata-contract violation" not in (out.explanation or "")
