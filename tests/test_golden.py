from __future__ import annotations

# Phase 5 — Golden regression tests
#
# Pins the observable, deterministic outputs of the full system so that any
# accidental regression in the Skill–Shadow–Recipe pipeline is caught
# immediately.
#
# Sections:
#   Intent → Recipe mapping          (CONTRACTUAL_CORRECTION, CREDIT_BLOCK, MASS_PRICING_ERROR)
#   Shadow rejection paths            (GREEN continue, YELLOW escalate, RED block)
#   FAIL_TO_HUMAN paths               (circuit breaker count, circuit breaker variance,
#                                      mass error no-recipe, recipe discount rejection)
#   Constrained output vocabulary     (AllowedIntent, AllowedRecipeName, ShadowStatus)
#   TraceRecord golden snapshot       (all fields present and correct after happy-path run)
#
# All tests are loop-free and deterministic (DeterministicFallbackBackend, no LLM).

import pytest

pytest.importorskip("langgraph", reason="langgraph not installed; skipping Phase 5 golden tests")

from contracts.models import GraphState, Intent, OrderEvent, TerminalStatus  # noqa: E402
from orchestration.graph import run_graph  # noqa: E402
from observability.tracer import Tracer  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _state(**kwargs) -> GraphState:
    defaults = dict(order_id="SO-G001", po_price=90.0, sap_base_price=100.0)
    defaults.update(kwargs)
    return GraphState(event=OrderEvent(**defaults))


# ---------------------------------------------------------------------------
# Golden: Intent → Recipe mapping
# ---------------------------------------------------------------------------

class TestGoldenIntentRecipeMapping:
    """Pins the exact intent→recipe pairing for every supported scenario."""

    def test_contractual_correction_selects_price_recipe(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.execution_log is not None
        assert result.execution_log.recipe_name == "PriceAdjustmentRecipe.py"

    def test_contractual_correction_intent_recorded_in_log(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.execution_log.intent_selected == "CONTRACTUAL_CORRECTION"

    def test_credit_block_selects_credit_recipe(self, credit_event):
        # Credit block routes through shadow YELLOW → MANUAL_REVIEW_REQUIRED
        # (no recipe executed); verify intent classification is still CREDIT_BLOCK.
        result = run_graph(credit_event)
        assert result.intent == Intent.CREDIT_BLOCK

    def test_credit_block_does_not_execute_recipe(self, credit_event):
        """CREDIT_BLOCK shadow → YELLOW → no recipe run."""
        result = run_graph(credit_event)
        assert result.execution_log is None

    def test_mass_error_has_no_recipe(self, mass_event):
        """MASS_PRICING_ERROR has no registered recipe → FAIL_TO_HUMAN."""
        result = run_graph(mass_event)
        assert result.execution_log is None

    def test_mass_error_intent_is_mass_pricing_error(self, mass_event):
        result = run_graph(mass_event)
        assert result.intent == Intent.MASS_PRICING_ERROR

    def test_price_recipe_output_contains_applied_condition(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.execution_log.outputs.get("applied_condition") == "YK07"

    def test_price_recipe_output_status_is_success(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.execution_log.outputs.get("status") == "SUCCESS"


# ---------------------------------------------------------------------------
# Golden: Shadow rejection paths
# ---------------------------------------------------------------------------

class TestGoldenShadowPaths:
    """Pins the exact terminal status for each shadow verdict colour."""

    def test_green_shadow_routes_to_complete(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.shadow is not None
        assert result.shadow.status.value == "GREEN"
        assert result.final_status == TerminalStatus.COMPLETE

    def test_yellow_shadow_routes_to_manual_review(self, credit_event):
        result = run_graph(credit_event)
        assert result.shadow is not None
        assert result.shadow.status.value == "YELLOW"
        assert result.final_status == TerminalStatus.MANUAL_REVIEW_REQUIRED

    def test_red_shadow_routes_to_blocked_or_fail(self, mass_event):
        result = run_graph(mass_event)
        assert result.shadow is not None
        assert result.shadow.status.value == "RED"
        assert result.final_status in {TerminalStatus.BLOCKED, TerminalStatus.FAIL_TO_HUMAN}

    def test_yellow_shadow_records_policy_hit(self, credit_event):
        result = run_graph(credit_event)
        assert "CREDIT_RELEASE_REVIEW" in result.shadow.policy_hits

    def test_green_shadow_has_no_policy_hits(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.shadow.policy_hits == []

    def test_shadow_trace_id_propagates_to_execution_log(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.execution_log.trace_id == result.shadow.trace_id

    def test_yellow_shadow_explanation_is_set(self, credit_event):
        result = run_graph(credit_event)
        assert result.explanation is not None and len(result.explanation) > 0

    def test_red_shadow_explanation_is_set(self, mass_event):
        result = run_graph(mass_event)
        assert result.explanation is not None and len(result.explanation) > 0


# ---------------------------------------------------------------------------
# Golden: FAIL_TO_HUMAN paths
# ---------------------------------------------------------------------------

class TestGoldenFailToHumanPaths:
    """Pins every distinct route to FAIL_TO_HUMAN."""

    def test_circuit_breaker_update_count_trips_fail_to_human(self):
        state = _state()
        state.update_count = 51
        result = run_graph(state)
        assert result.final_status == TerminalStatus.FAIL_TO_HUMAN

    def test_circuit_breaker_count_explanation_contains_50_or_updates(self):
        state = _state()
        state.update_count = 51
        result = run_graph(state)
        assert "50" in result.explanation or "updates" in result.explanation.lower()

    def test_circuit_breaker_count_no_shadow_no_recipe(self):
        state = _state()
        state.update_count = 51
        result = run_graph(state)
        assert result.shadow is None
        assert result.execution_log is None

    def test_circuit_breaker_variance_trips_fail_to_human(self):
        # po_price=0, sap_base_price=100, line_count=200 → variance=20000 > 10000
        state = _state()
        state.event = OrderEvent(
            order_id="SO-CB02", po_price=0.0, sap_base_price=100.0, line_count=200
        )
        result = run_graph(state)
        assert result.final_status == TerminalStatus.FAIL_TO_HUMAN

    def test_circuit_breaker_variance_no_shadow(self):
        state = _state()
        state.event = OrderEvent(
            order_id="SO-CB03", po_price=0.0, sap_base_price=100.0, line_count=200
        )
        result = run_graph(state)
        assert result.shadow is None

    def test_mass_error_no_recipe_routes_to_fail_to_human(self, mass_event):
        result = run_graph(mass_event)
        assert result.final_status in {TerminalStatus.FAIL_TO_HUMAN, TerminalStatus.BLOCKED}

    def test_recipe_discount_rejection_routes_to_fail_to_human(self):
        """50% discount exceeds 15% threshold → recipe FAILED → FAIL_TO_HUMAN."""
        state = _state(order_id="SO-DISC01", po_price=50.0, sap_base_price=100.0)
        result = run_graph(state)
        assert result.final_status == TerminalStatus.FAIL_TO_HUMAN

    def test_recipe_discount_rejection_execution_log_status_is_failed(self):
        state = _state(order_id="SO-DISC02", po_price=50.0, sap_base_price=100.0)
        result = run_graph(state)
        assert result.execution_log is not None
        assert result.execution_log.outputs.get("status") == "FAILED"

    def test_recipe_discount_rejection_explanation_is_set(self):
        state = _state(order_id="SO-DISC03", po_price=50.0, sap_base_price=100.0)
        result = run_graph(state)
        assert result.explanation is not None


# ---------------------------------------------------------------------------
# Golden: Constrained output vocabulary
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Golden: DUPLICATE_PO intent → recipe mapping (TEST-1 fix)
# ---------------------------------------------------------------------------

class TestGoldenDuplicatePO:
    """Pins DUPLICATE_PO intent → DuplicatePORecipe.py end-to-end."""

    def test_duplicate_po_intent_classified(self, duplicate_po_event):
        result = run_graph(duplicate_po_event)
        assert result.intent == Intent.DUPLICATE_PO

    def test_duplicate_po_selects_recipe(self, duplicate_po_event):
        result = run_graph(duplicate_po_event)
        assert result.execution_log is not None
        assert result.execution_log.recipe_name == "DuplicatePORecipe.py"

    def test_duplicate_po_shadow_is_green(self, duplicate_po_event):
        result = run_graph(duplicate_po_event)
        assert result.shadow.status.value == "GREEN"

    def test_duplicate_po_high_signals_blocked(self, duplicate_po_event):
        result = run_graph(duplicate_po_event)
        assert result.final_status == TerminalStatus.BLOCKED

    def test_duplicate_po_output_has_composite_score(self, duplicate_po_event):
        result = run_graph(duplicate_po_event)
        assert "composite_score" in result.execution_log.outputs

    def test_duplicate_po_output_has_signal_breakdown(self, duplicate_po_event):
        result = run_graph(duplicate_po_event)
        assert "signal_breakdown" in result.execution_log.outputs

    def test_duplicate_po_low_signals_complete(self):
        state = _state(
            order_id="PO-GLOW01",
            po_price=100.0,
            sap_base_price=100.0,
            event_type="EDI_850_DUPLICATE_PO",
            retailer_id="R-10",
        )
        state.event.metadata = {"signal_scores": {}}
        result = run_graph(state)
        assert result.final_status == TerminalStatus.COMPLETE
        assert result.execution_log.outputs.get("classification") == "PASS"

    def test_duplicate_po_trace_record(self, duplicate_po_event):
        result = run_graph(duplicate_po_event)
        record = Tracer().build_record(result)
        assert record.intent_selected == "DUPLICATE_PO"
        assert record.recipe_name == "DuplicatePORecipe.py"


class TestGoldenConstrainedVocabulary:
    """Every constrained field must use only the permitted vocabulary."""

    ALLOWED_INTENTS = {
        "CONTRACTUAL_CORRECTION", "CREDIT_BLOCK", "MASS_PRICING_ERROR",
        "DUPLICATE_PO", "PRICE_HOLD_RELEASE", "EDI_MISMATCH",
    }
    ALLOWED_RECIPES = {
        "PriceAdjustmentRecipe.py", "CreditHoldReleaseRecipe.py",
        "DuplicatePORecipe.py", "PriceHoldReleaseRecipe.py",
        "EdiMismatchRecipe.py", None,
    }
    ALLOWED_VERDICTS = {"GREEN", "YELLOW", "RED"}
    ALLOWED_STATUSES = {s.value for s in TerminalStatus}

    def test_intent_within_vocabulary(self, pricing_event, credit_event, mass_event):
        for state in (pricing_event, credit_event, mass_event):
            result = run_graph(state)
            assert result.intent.value in self.ALLOWED_INTENTS

    def test_shadow_verdict_within_vocabulary(self, pricing_event, credit_event, mass_event):
        for state in (pricing_event, credit_event, mass_event):
            result = run_graph(state)
            assert result.shadow.status.value in self.ALLOWED_VERDICTS

    def test_final_status_within_vocabulary(self, pricing_event, credit_event, mass_event):
        for state in (pricing_event, credit_event, mass_event):
            result = run_graph(state)
            assert result.final_status.value in self.ALLOWED_STATUSES

    def test_execution_log_constrained_outputs_intent_schema(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.execution_log.constrained_outputs.get("intent") == "IntentDecision"

    def test_execution_log_constrained_outputs_shadow_schema(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.execution_log.constrained_outputs.get("shadow") == "ShadowDecisionSchema"

    def test_execution_log_constrained_outputs_recipe_schema(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.execution_log.constrained_outputs.get("recipe") == "RecipeProposal"


# ---------------------------------------------------------------------------
# Golden: TraceRecord snapshot (happy path)
# ---------------------------------------------------------------------------

class TestGoldenTraceRecord:
    """After a happy-path run, the TraceRecord must be fully populated."""

    def test_trace_record_trace_id_is_set(self, pricing_event):
        result = run_graph(pricing_event)
        record = Tracer().build_record(result)
        assert record.trace_id and len(record.trace_id) > 0

    def test_trace_record_event_id_matches_order(self, pricing_event):
        result = run_graph(pricing_event)
        record = Tracer().build_record(result)
        assert record.event_id == "SO-1001"

    def test_trace_record_skill_name_is_pricing_reconciliation(self, pricing_event):
        result = run_graph(pricing_event)
        record = Tracer().build_record(result)
        assert record.skill_name == "pricing-reconciliation"

    def test_trace_record_intent_is_contractual_correction(self, pricing_event):
        result = run_graph(pricing_event)
        record = Tracer().build_record(result)
        assert record.intent_selected == "CONTRACTUAL_CORRECTION"

    def test_trace_record_shadow_verdict_is_green(self, pricing_event):
        result = run_graph(pricing_event)
        record = Tracer().build_record(result)
        assert record.shadow_verdict == "GREEN"

    def test_trace_record_recipe_name_is_price_adjustment(self, pricing_event):
        result = run_graph(pricing_event)
        record = Tracer().build_record(result)
        assert record.recipe_name == "PriceAdjustmentRecipe.py"

    def test_trace_record_final_status_is_complete(self, pricing_event):
        result = run_graph(pricing_event)
        record = Tracer().build_record(result)
        assert record.final_status == "COMPLETE"

    def test_trace_record_constrained_schemas_set(self, pricing_event):
        result = run_graph(pricing_event)
        record = Tracer().build_record(result)
        assert record.constrained_output_schemas.get("intent") == "IntentDecision"
        assert record.constrained_output_schemas.get("shadow") == "ShadowDecisionSchema"
        assert record.constrained_output_schemas.get("recipe") == "RecipeProposal"

    def test_trace_record_is_json_serialisable(self, pricing_event):
        import json
        result = run_graph(pricing_event)
        record = Tracer().build_record(result)
        parsed = json.loads(record.to_json())
        assert parsed["final_status"] == "COMPLETE"

    def test_trace_record_shadow_policy_hits_empty_on_green(self, pricing_event):
        result = run_graph(pricing_event)
        record = Tracer().build_record(result)
        assert record.shadow_policy_hits == []

    def test_trace_record_yellow_path_fields(self, credit_event):
        result = run_graph(credit_event)
        record = Tracer().build_record(result)
        assert record.shadow_verdict == "YELLOW"
        assert "CREDIT_RELEASE_REVIEW" in record.shadow_policy_hits
        assert record.final_status == "MANUAL_REVIEW_REQUIRED"
        assert record.recipe_name is None
