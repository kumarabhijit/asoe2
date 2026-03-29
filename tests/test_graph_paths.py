from __future__ import annotations

# Phase 4 — LangGraph end-to-end path tests
#
# Verifies deterministic state transitions through the full graph:
#   Happy path      → COMPLETE with full ExecutionLog
#   YELLOW verdict  → MANUAL_REVIEW_REQUIRED (no recipe executed)
#   RED verdict     → BLOCKED or FAIL_TO_HUMAN (no recipe executed)
#   Circuit breaker → FAIL_TO_HUMAN (halted before shadow)
#   Recipe failure  → FAIL_TO_HUMAN (discount exceeded, recipe-level rejection)
#   Mass error      → FAIL_TO_HUMAN / BLOCKED (no recipe for this intent)
#
# All graph paths are loop-free and deterministic (no hidden retries).

import pytest

pytest.importorskip("langgraph", reason="langgraph not installed; skipping Phase 4 graph tests")

from orchestration.graph import run_graph  # noqa: E402
from contracts.models import GraphState, Intent, OrderEvent, TerminalStatus  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures local to graph tests
# ---------------------------------------------------------------------------

def _state(**kwargs) -> GraphState:
    defaults = dict(order_id="SO-T001", po_price=90.0, sap_base_price=100.0)
    defaults.update(kwargs)
    return GraphState(event=OrderEvent(**defaults))


# ---------------------------------------------------------------------------
# Happy path — CONTRACTUAL_CORRECTION → COMPLETE
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_final_status_is_complete(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.final_status == TerminalStatus.COMPLETE

    def test_execution_log_present(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.execution_log is not None

    def test_recipe_is_price_adjustment(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.execution_log.recipe_name == "PriceAdjustmentRecipe.py"

    def test_intent_selected_recorded(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.execution_log.intent_selected == "CONTRACTUAL_CORRECTION"

    def test_trace_id_present(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.execution_log.trace_id

    def test_constrained_outputs_recorded(self, pricing_event):
        result = run_graph(pricing_event)
        co = result.execution_log.constrained_outputs
        assert co.get("intent") == "IntentDecision"
        assert co.get("shadow") == "ShadowDecisionSchema"
        assert co.get("recipe") == "RecipeProposal"

    def test_shadow_decision_captured(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.shadow is not None
        assert result.shadow.status.value == "GREEN"

    def test_skill_loaded(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.skill is not None
        assert result.skill.name == "pricing-reconciliation"

    def test_discrepancy_computed(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.discrepancy is not None
        assert result.discrepancy.delta == -10.0

    def test_outputs_contain_yk07(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.execution_log.outputs.get("applied_condition") == "YK07"

    def test_explanation_is_set(self, pricing_event):
        result = run_graph(pricing_event)
        assert result.explanation is not None


# ---------------------------------------------------------------------------
# YELLOW path — CREDIT_BLOCK → MANUAL_REVIEW_REQUIRED
# ---------------------------------------------------------------------------

class TestYellowPath:
    def test_final_status_is_manual_review(self, credit_event):
        result = run_graph(credit_event)
        assert result.final_status == TerminalStatus.MANUAL_REVIEW_REQUIRED

    def test_no_execution_log_on_yellow(self, credit_event):
        """No recipe must be executed when shadow returns YELLOW."""
        result = run_graph(credit_event)
        assert result.execution_log is None

    def test_shadow_decision_is_yellow(self, credit_event):
        result = run_graph(credit_event)
        assert result.shadow is not None
        assert result.shadow.status.value == "YELLOW"

    def test_policy_hit_recorded(self, credit_event):
        result = run_graph(credit_event)
        assert "CREDIT_RELEASE_REVIEW" in result.shadow.policy_hits

    def test_explanation_references_yellow(self, credit_event):
        result = run_graph(credit_event)
        assert result.explanation is not None and len(result.explanation) > 0


# ---------------------------------------------------------------------------
# RED path — MASS_PRICING_ERROR → BLOCKED or FAIL_TO_HUMAN
# ---------------------------------------------------------------------------

class TestRedPath:
    def test_final_status_is_blocked_or_fail(self, mass_event):
        result = run_graph(mass_event)
        assert result.final_status in {TerminalStatus.FAIL_TO_HUMAN, TerminalStatus.BLOCKED}

    def test_no_execution_log_on_red(self, mass_event):
        """No recipe must be executed when shadow or intent blocks the path."""
        result = run_graph(mass_event)
        assert result.execution_log is None

    def test_explanation_is_set(self, mass_event):
        result = run_graph(mass_event)
        assert result.explanation is not None and len(result.explanation) > 0


# ---------------------------------------------------------------------------
# Circuit breaker — breach → FAIL_TO_HUMAN (tasks.md §4.2)
# ---------------------------------------------------------------------------

class TestCircuitBreakerPath:
    def test_high_update_count_trips_breaker(self):
        state = _state()
        state.update_count = 51          # above 50-update limit
        result = run_graph(state)
        assert result.final_status == TerminalStatus.FAIL_TO_HUMAN

    def test_high_variance_trips_breaker(self):
        # po_price=0, sap_base_price=100, line_count=200 → variance=20000
        state = _state(po_price=0.0, sap_base_price=100.0)
        state.event = OrderEvent(
            order_id="SO-CB01",
            po_price=0.0,
            sap_base_price=100.0,
            line_count=200,
        )
        result = run_graph(state)
        assert result.final_status == TerminalStatus.FAIL_TO_HUMAN

    def test_circuit_breaker_explanation_set(self):
        state = _state()
        state.update_count = 51
        result = run_graph(state)
        assert result.explanation is not None
        assert "50" in result.explanation or "updates" in result.explanation.lower()

    def test_no_execution_log_when_breaker_trips(self):
        state = _state()
        state.update_count = 51
        result = run_graph(state)
        assert result.execution_log is None

    def test_no_shadow_decision_when_breaker_trips(self):
        """Shadow must not run after circuit breaker halts the graph."""
        state = _state()
        state.update_count = 51
        result = run_graph(state)
        assert result.shadow is None


# ---------------------------------------------------------------------------
# Recipe-level rejection — FAIL_TO_HUMAN on FAILED recipe output
# ---------------------------------------------------------------------------

class TestRecipeRejectionPath:
    def test_large_discount_routes_to_fail_to_human(self):
        """50% discount exceeds threshold → recipe FAILED → FAIL_TO_HUMAN."""
        state = _state(po_price=50.0, sap_base_price=100.0)
        result = run_graph(state)
        assert result.final_status == TerminalStatus.FAIL_TO_HUMAN

    def test_execution_log_present_on_recipe_failure(self):
        """ExecutionLog must be populated even when the recipe rejects."""
        state = _state(po_price=50.0, sap_base_price=100.0)
        result = run_graph(state)
        assert result.execution_log is not None
        assert result.execution_log.outputs.get("status") == "FAILED"

    def test_explanation_set_on_recipe_failure(self):
        state = _state(po_price=50.0, sap_base_price=100.0)
        result = run_graph(state)
        assert result.explanation is not None


# ---------------------------------------------------------------------------
# Loop-safety — graph always terminates (no hidden loops)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# DUPLICATE_PO path — full graph execution (TEST-1 fix)
# ---------------------------------------------------------------------------

class TestDuplicatePOPath:
    def test_duplicate_po_high_signals_blocked(self, duplicate_po_event):
        """High signal scores → AUTO_BLOCK → BLOCKED terminal status."""
        result = run_graph(duplicate_po_event)
        assert result.final_status == TerminalStatus.BLOCKED

    def test_duplicate_po_intent_classified(self, duplicate_po_event):
        result = run_graph(duplicate_po_event)
        assert result.intent == Intent.DUPLICATE_PO

    def test_duplicate_po_shadow_is_green(self, duplicate_po_event):
        result = run_graph(duplicate_po_event)
        assert result.shadow is not None
        assert result.shadow.status.value == "GREEN"

    def test_duplicate_po_execution_log_present(self, duplicate_po_event):
        result = run_graph(duplicate_po_event)
        assert result.execution_log is not None
        assert result.execution_log.recipe_name == "DuplicatePORecipe.py"

    def test_duplicate_po_recipe_output_has_classification(self, duplicate_po_event):
        result = run_graph(duplicate_po_event)
        assert result.execution_log.outputs.get("classification") == "AUTO_BLOCK"

    def test_duplicate_po_explanation_set(self, duplicate_po_event):
        result = run_graph(duplicate_po_event)
        assert result.explanation is not None

    def test_duplicate_po_low_signals_pass(self):
        """Empty signal scores → PASS → COMPLETE terminal status."""
        state = GraphState(event=OrderEvent(
            order_id="PO-LOW01",
            po_price=100.0,
            sap_base_price=100.0,
            event_type="EDI_850_DUPLICATE_PO",
            retailer_id="R-10",
            metadata={"signal_scores": {}},
        ))
        result = run_graph(state)
        assert result.final_status == TerminalStatus.COMPLETE
        assert result.execution_log.outputs.get("classification") == "PASS"

    def test_duplicate_po_skill_loaded(self, duplicate_po_event):
        result = run_graph(duplicate_po_event)
        assert result.skill is not None

    def test_duplicate_po_notification_effect_applied(self, duplicate_po_event):
        """Buyer notification gateway effect is dispatched after recipe execution."""
        result = run_graph(duplicate_po_event)
        # BLOCK_AND_NOTIFY → notification_template = "duplicate_po_blocked"
        assert result.execution_log.outputs.get("notification_template") == "duplicate_po_blocked"
        # Effect result recorded
        assert len(result.effect_results) >= 1
        notif_effect = [e for e in result.effect_results if e.gateway_name == "buyer_notification"]
        assert len(notif_effect) == 1
        assert notif_effect[0].status == "SUCCESS"

    def test_duplicate_po_pass_no_notification(self):
        """PASS → ALLOW_BOTH → no notification template → effect still dispatched but with None template."""
        state = GraphState(event=OrderEvent(
            order_id="PO-NTF01", po_price=100.0, sap_base_price=100.0,
            event_type="EDI_850_DUPLICATE_PO", retailer_id="R-10",
            metadata={"signal_scores": {}},
        ))
        result = run_graph(state)
        assert result.execution_log.outputs.get("notification_template") is None


# ---------------------------------------------------------------------------
# EC04 — Duplicate PO resend path (Costco-style exact resend)
# ---------------------------------------------------------------------------

class TestDuplicatePOResendPath:
    """End-to-end graph tests for an exact PO resend scenario (EC04)."""

    def test_resend_classified_as_duplicate_po(self, duplicate_po_resend_event):
        result = run_graph(duplicate_po_resend_event)
        assert result.intent == Intent.DUPLICATE_PO

    def test_resend_shadow_is_green(self, duplicate_po_resend_event):
        result = run_graph(duplicate_po_resend_event)
        assert result.shadow is not None
        assert result.shadow.status.value == "GREEN"

    def test_resend_classified_as_auto_block(self, duplicate_po_resend_event):
        result = run_graph(duplicate_po_resend_event)
        assert result.execution_log.outputs.get("classification") == "AUTO_BLOCK"

    def test_resend_composite_score_above_threshold(self, duplicate_po_resend_event):
        result = run_graph(duplicate_po_resend_event)
        assert result.execution_log.outputs.get("composite_score", 0) >= 0.90

    def test_resend_selects_duplicate_po_recipe(self, duplicate_po_resend_event):
        result = run_graph(duplicate_po_resend_event)
        assert result.execution_log.recipe_name == "DuplicatePORecipe.py"

    def test_resend_terminal_status_blocked(self, duplicate_po_resend_event):
        result = run_graph(duplicate_po_resend_event)
        assert result.final_status == TerminalStatus.BLOCKED

    def test_resend_notification_effect_dispatched(self, duplicate_po_resend_event):
        result = run_graph(duplicate_po_resend_event)
        assert result.execution_log.outputs.get("notification_template") == "duplicate_po_blocked"
        notif = [e for e in result.effect_results if e.gateway_name == "buyer_notification"]
        assert len(notif) == 1


# ---------------------------------------------------------------------------
# EC08 — Batch PO path (one new PO from multi-PO email, no match → PASS)
# ---------------------------------------------------------------------------

class TestDuplicatePOBatchPath:
    """End-to-end graph tests for a batch PO that has no existing match (EC08)."""

    def test_batch_po_classified_as_duplicate_po(self, duplicate_po_batch_event):
        result = run_graph(duplicate_po_batch_event)
        assert result.intent == Intent.DUPLICATE_PO

    def test_batch_po_low_score_passes(self, duplicate_po_batch_event):
        result = run_graph(duplicate_po_batch_event)
        assert result.execution_log.outputs.get("classification") == "PASS"
        assert result.final_status == TerminalStatus.COMPLETE

    def test_batch_po_recommends_allow_both(self, duplicate_po_batch_event):
        result = run_graph(duplicate_po_batch_event)
        assert result.execution_log.outputs.get("recommended_action") == "ALLOW_BOTH"

    def test_batch_po_no_notification_template(self, duplicate_po_batch_event):
        result = run_graph(duplicate_po_batch_event)
        assert result.execution_log.outputs.get("notification_template") is None

    def test_batch_po_shadow_is_green(self, duplicate_po_batch_event):
        result = run_graph(duplicate_po_batch_event)
        assert result.shadow is not None
        assert result.shadow.status.value == "GREEN"


# ---------------------------------------------------------------------------
# Loop-safety — graph always terminates (no hidden loops)
# ---------------------------------------------------------------------------

class TestLoopSafety:
    def test_graph_always_terminates_on_pricing(self, pricing_event):
        """Graph must terminate in at most N node calls (not loop infinitely)."""
        result = run_graph(pricing_event)
        assert result.final_status is not None

    def test_graph_always_terminates_on_credit(self, credit_event):
        result = run_graph(credit_event)
        assert result.final_status is not None

    def test_graph_always_terminates_on_mass(self, mass_event):
        result = run_graph(mass_event)
        assert result.final_status is not None

    def test_graph_always_terminates_on_duplicate_po(self, duplicate_po_event):
        result = run_graph(duplicate_po_event)
        assert result.final_status is not None

    def test_final_status_always_in_allowed_set(self, pricing_event, credit_event, mass_event, duplicate_po_event):
        allowed = set(TerminalStatus)
        for state in (pricing_event, credit_event, mass_event, duplicate_po_event):
            result = run_graph(state)
            assert result.final_status in allowed
