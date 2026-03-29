from __future__ import annotations

# Phase 4 — Node unit tests
#
# Each LangGraph node is a plain Python function (GraphState → GraphState).
# These tests exercise every node in isolation — no full graph invocation,
# no langgraph import needed — so they always run regardless of whether
# langgraph is installed.
#
# Covered:
#   ingest              — update_count increment, batch_total_variance
#   classify            — intent set, confidence set, discrepancy computed
#   load_skill          — skill loaded verbatim for EDI_850 events
#   validate_circuit_breaker — pass-through and breach → FAIL_TO_HUMAN
#   shadow_audit        — GREEN continue, YELLOW MANUAL_REVIEW, RED BLOCKED
#   select_recipe       — proposal → selected_recipe; None → FAIL_TO_HUMAN
#   validate_types      — RecipeInvocation built for each recipe
#   execute_recipe      — success, recipe rejection, missing invocation guard

import pytest

from contracts.models import (
    ComplianceDecision,
    GraphState,
    Intent,
    OrderEvent,
    RecipeInvocation,
    ShadowStatus,
    TerminalStatus,
)
from orchestration.nodes import (
    classify,
    execute_recipe,
    ingest,
    load_skill,
    select_recipe,
    shadow_audit,
    validate_circuit_breaker,
    validate_types,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _state(**kwargs) -> GraphState:
    defaults = dict(order_id="SO-T001", po_price=90.0, sap_base_price=100.0)
    defaults.update(kwargs)
    return GraphState(event=OrderEvent(**defaults))


def _green_shadow() -> ComplianceDecision:
    return ComplianceDecision(
        status=ShadowStatus.GREEN,
        reasons=["No blocking policy hit."],
        policy_hits=[],
        constrained_by="Guidance/Outlines fallback schema",
    )


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

class TestIngestNode:
    def test_increments_update_count(self):
        state = _state()
        assert state.update_count == 0
        result = ingest(state)
        assert result.update_count == 1

    def test_computes_batch_variance(self):
        state = _state(po_price=90.0, sap_base_price=100.0, line_count=5)
        result = ingest(state)
        assert result.batch_total_variance == 50.0   # |90 - 100| * 5

    def test_variance_uses_abs(self):
        state = _state(po_price=110.0, sap_base_price=100.0)
        result = ingest(state)
        assert result.batch_total_variance >= 0.0

    def test_returns_graph_state(self):
        result = ingest(_state())
        assert isinstance(result, GraphState)


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------

class TestClassifyNode:
    def test_sets_intent(self, pricing_event):
        result = classify(pricing_event)
        assert result.intent != Intent.UNKNOWN

    def test_intent_within_allowed(self, pricing_event):
        result = classify(pricing_event)
        assert result.intent in {
            Intent.CONTRACTUAL_CORRECTION,
            Intent.CREDIT_BLOCK,
            Intent.MASS_PRICING_ERROR,
        }

    def test_sets_confidence(self, pricing_event):
        result = classify(pricing_event)
        assert 0.0 <= result.confidence <= 1.0

    def test_sets_discrepancy(self, pricing_event):
        result = classify(pricing_event)
        assert result.discrepancy is not None

    def test_discrepancy_delta_correct(self, pricing_event):
        result = classify(pricing_event)
        assert result.discrepancy.delta == -10.0   # 90 - 100

    def test_pricing_event_classifies_contractual(self, pricing_event):
        result = classify(pricing_event)
        assert result.intent == Intent.CONTRACTUAL_CORRECTION

    def test_credit_event_classifies_credit_block(self, credit_event):
        result = classify(credit_event)
        assert result.intent == Intent.CREDIT_BLOCK

    def test_mass_event_classifies_mass_error(self, mass_event):
        result = classify(mass_event)
        assert result.intent == Intent.MASS_PRICING_ERROR


# ---------------------------------------------------------------------------
# load_skill
# ---------------------------------------------------------------------------

class TestLoadSkillNode:
    def test_skill_is_set(self, pricing_event):
        result = load_skill(pricing_event)
        assert result.skill is not None

    def test_skill_name_is_pricing_reconciliation(self, pricing_event):
        result = load_skill(pricing_event)
        assert result.skill.name == "pricing-reconciliation"

    def test_skill_text_contains_compliance_shadow(self, pricing_event):
        result = load_skill(pricing_event)
        assert "Compliance Shadow" in result.skill.text

    def test_skill_lists_price_recipe(self, pricing_event):
        result = load_skill(pricing_event)
        assert "PriceAdjustmentRecipe.py" in result.skill.recipes

    def test_returns_graph_state(self, pricing_event):
        result = load_skill(pricing_event)
        assert isinstance(result, GraphState)


# ---------------------------------------------------------------------------
# validate_circuit_breaker
# ---------------------------------------------------------------------------

class TestCircuitBreakerNode:
    def test_normal_event_passes_through(self, pricing_event):
        pricing_event.update_count = 1
        pricing_event.batch_total_variance = 100.0
        result = validate_circuit_breaker(pricing_event)
        assert result.final_status is None

    def test_update_count_exceeded_sets_fail_to_human(self):
        state = _state()
        state.update_count = 51
        result = validate_circuit_breaker(state)
        assert result.final_status == TerminalStatus.FAIL_TO_HUMAN

    def test_variance_exceeded_sets_fail_to_human(self):
        state = _state()
        state.batch_total_variance = 10_001.0
        result = validate_circuit_breaker(state)
        assert result.final_status == TerminalStatus.FAIL_TO_HUMAN

    def test_breach_explanation_is_set(self):
        state = _state()
        state.update_count = 51
        result = validate_circuit_breaker(state)
        assert result.explanation is not None and len(result.explanation) > 0

    def test_both_breaches_both_reasons_in_explanation(self):
        state = _state()
        state.update_count = 51
        state.batch_total_variance = 15_000.0
        result = validate_circuit_breaker(state)
        assert "50" in result.explanation or "updates" in result.explanation.lower()

    def test_at_boundary_50_updates_passes(self):
        state = _state()
        state.update_count = 50    # exactly 50 — NOT above 50
        state.batch_total_variance = 0.0
        result = validate_circuit_breaker(state)
        assert result.final_status is None


# ---------------------------------------------------------------------------
# shadow_audit
# ---------------------------------------------------------------------------

class TestShadowAuditNode:
    def test_green_leaves_final_status_none(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        result = shadow_audit(pricing_event)
        assert result.final_status is None

    def test_green_sets_shadow_decision(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        result = shadow_audit(pricing_event)
        assert result.shadow is not None
        assert result.shadow.status == ShadowStatus.GREEN

    def test_yellow_sets_manual_review(self, credit_event):
        credit_event.intent = Intent.CREDIT_BLOCK
        result = shadow_audit(credit_event)
        assert result.final_status == TerminalStatus.MANUAL_REVIEW_REQUIRED

    def test_yellow_sets_shadow_and_explanation(self, credit_event):
        credit_event.intent = Intent.CREDIT_BLOCK
        result = shadow_audit(credit_event)
        assert result.shadow is not None
        assert result.explanation is not None

    def test_red_sets_blocked(self, mass_event):
        mass_event.intent = Intent.MASS_PRICING_ERROR
        result = shadow_audit(mass_event)
        assert result.final_status == TerminalStatus.BLOCKED

    def test_red_sets_shadow_and_explanation(self, mass_event):
        mass_event.intent = Intent.MASS_PRICING_ERROR
        result = shadow_audit(mass_event)
        assert result.shadow is not None
        assert result.explanation is not None

    def test_shadow_trace_id_is_set(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        result = shadow_audit(pricing_event)
        assert result.shadow.trace_id


# ---------------------------------------------------------------------------
# select_recipe
# ---------------------------------------------------------------------------

class TestSelectRecipeNode:
    def test_contractual_selects_price_recipe(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        result = select_recipe(pricing_event)
        assert result.selected_recipe == "PriceAdjustmentRecipe.py"

    def test_credit_block_selects_credit_recipe(self, credit_event):
        credit_event.intent = Intent.CREDIT_BLOCK
        result = select_recipe(credit_event)
        assert result.selected_recipe == "CreditHoldReleaseRecipe.py"

    def test_mass_error_routes_to_fail_to_human(self, mass_event):
        mass_event.intent = Intent.MASS_PRICING_ERROR
        result = select_recipe(mass_event)
        assert result.final_status == TerminalStatus.FAIL_TO_HUMAN

    def test_mass_error_explanation_set(self, mass_event):
        mass_event.intent = Intent.MASS_PRICING_ERROR
        result = select_recipe(mass_event)
        assert result.explanation is not None

    def test_selected_recipe_within_registry(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        result = select_recipe(pricing_event)
        from recipes.registry import REGISTRY
        assert result.selected_recipe in REGISTRY


# ---------------------------------------------------------------------------
# validate_types
# ---------------------------------------------------------------------------

class TestValidateTypesNode:
    def _price_state(self) -> GraphState:
        state = _state(po_price=92.0, sap_base_price=100.0)
        state.selected_recipe = "PriceAdjustmentRecipe.py"
        return state

    def _credit_state(self) -> GraphState:
        state = GraphState(event=OrderEvent(
            order_id="SO-2001", po_price=100.0, sap_base_price=100.0,
            requester_role="ORDER_MANAGER",
            credit_limit=10_000.0, current_exposure=9_500.0,
        ))
        state.selected_recipe = "CreditHoldReleaseRecipe.py"
        return state

    def test_price_recipe_builds_invocation(self):
        result = validate_types(self._price_state())
        assert result.invocation is not None
        assert result.invocation.recipe_name == "PriceAdjustmentRecipe.py"

    def test_price_invocation_contains_required_params(self):
        result = validate_types(self._price_state())
        for param in ("order_id", "line_item", "requested_price", "erp_context"):
            assert param in result.invocation.params

    def test_price_invocation_erp_context_has_base_price(self):
        result = validate_types(self._price_state())
        assert result.invocation.params["erp_context"]["base_price"] == 100.0

    def test_price_invocation_max_discount_is_15_pct(self):
        result = validate_types(self._price_state())
        assert result.invocation.params["erp_context"]["max_discount_allowed"] == 0.15

    def test_credit_recipe_builds_invocation(self):
        result = validate_types(self._credit_state())
        assert result.invocation is not None
        assert result.invocation.recipe_name == "CreditHoldReleaseRecipe.py"

    def test_credit_invocation_contains_required_params(self):
        result = validate_types(self._credit_state())
        for param in ("order_id", "requester_role", "credit_limit", "current_exposure"):
            assert param in result.invocation.params

    def test_no_recipe_leaves_invocation_none(self):
        state = _state()
        state.selected_recipe = None
        result = validate_types(state)
        assert result.invocation is None


# ---------------------------------------------------------------------------
# execute_recipe
# ---------------------------------------------------------------------------

class TestExecuteRecipeNode:
    def _price_invocation_state(self, po_price=92.0) -> GraphState:
        state = _state(po_price=po_price, sap_base_price=100.0)
        state.intent = Intent.CONTRACTUAL_CORRECTION
        state.shadow = _green_shadow()
        state.invocation = RecipeInvocation(
            recipe_name="PriceAdjustmentRecipe.py",
            params={
                "order_id": "SO-T001",
                "line_item": 1,
                "requested_price": po_price,
                "erp_context": {
                    "base_price": 100.0,
                    "max_discount_allowed": 0.15,
                    "condition_type": "YK07",
                },
            },
        )
        return state

    def _credit_invocation_state(self, role="ORDER_MANAGER", exposure=9_000.0) -> GraphState:
        state = GraphState(event=OrderEvent(
            order_id="SO-T002", po_price=100.0, sap_base_price=100.0,
            requester_role=role, credit_limit=10_000.0, current_exposure=exposure,
        ))
        state.intent = Intent.CREDIT_BLOCK
        state.shadow = _green_shadow()
        state.invocation = RecipeInvocation(
            recipe_name="CreditHoldReleaseRecipe.py",
            params={
                "order_id": "SO-T002",
                "requester_role": role,
                "credit_limit": 10_000.0,
                "current_exposure": exposure,
                "authorized_roles": ("ORDER_MANAGER", "FINANCE_DIRECTOR"),
                "exposure_tolerance": 5_000.0,
            },
        )
        return state

    # -- success paths -------------------------------------------------------

    def test_price_success_status_complete(self):
        result = execute_recipe(self._price_invocation_state(po_price=92.0))
        assert result.final_status == TerminalStatus.COMPLETE

    def test_price_success_execution_log_present(self):
        result = execute_recipe(self._price_invocation_state(po_price=92.0))
        assert result.execution_log is not None

    def test_price_success_recipe_name_in_log(self):
        result = execute_recipe(self._price_invocation_state(po_price=92.0))
        assert result.execution_log.recipe_name == "PriceAdjustmentRecipe.py"

    def test_price_success_trace_id_from_shadow(self):
        state = self._price_invocation_state(po_price=92.0)
        shadow_trace = state.shadow.trace_id
        result = execute_recipe(state)
        assert result.execution_log.trace_id == shadow_trace

    def test_price_success_constrained_outputs_set(self):
        result = execute_recipe(self._price_invocation_state(po_price=92.0))
        co = result.execution_log.constrained_outputs
        assert co.get("intent") == "IntentDecision"
        assert co.get("recipe") == "RecipeProposal"

    def test_price_success_intent_in_log(self):
        result = execute_recipe(self._price_invocation_state(po_price=92.0))
        assert result.execution_log.intent_selected == "CONTRACTUAL_CORRECTION"

    def test_credit_success_status_complete(self):
        result = execute_recipe(self._credit_invocation_state())
        assert result.final_status == TerminalStatus.COMPLETE

    def test_credit_success_execution_log_present(self):
        result = execute_recipe(self._credit_invocation_state())
        assert result.execution_log is not None

    # -- recipe-level rejection paths ----------------------------------------

    def test_large_discount_routes_to_fail_to_human(self):
        result = execute_recipe(self._price_invocation_state(po_price=50.0))
        assert result.final_status == TerminalStatus.FAIL_TO_HUMAN

    def test_large_discount_execution_log_has_failed_output(self):
        result = execute_recipe(self._price_invocation_state(po_price=50.0))
        assert result.execution_log is not None
        assert result.execution_log.outputs.get("status") == "FAILED"

    def test_unauthorized_credit_role_routes_to_blocked(self):
        result = execute_recipe(self._credit_invocation_state(role="CSR"))
        assert result.final_status == TerminalStatus.BLOCKED

    def test_exposure_exceeded_routes_to_rejected(self):
        result = execute_recipe(self._credit_invocation_state(exposure=20_000.0))
        assert result.final_status == TerminalStatus.REJECTED

    # -- missing invocation guard --------------------------------------------

    def test_none_invocation_routes_to_fail_to_human(self):
        state = _state()
        state.invocation = None
        result = execute_recipe(state)
        assert result.final_status == TerminalStatus.FAIL_TO_HUMAN

    def test_none_invocation_explanation_set(self):
        state = _state()
        state.invocation = None
        result = execute_recipe(state)
        assert result.explanation is not None

    def test_none_invocation_no_execution_log(self):
        state = _state()
        state.invocation = None
        result = execute_recipe(state)
        assert result.execution_log is None

    # -- skill_name and shadow_verdict in ExecutionLog (output contract) ------

    def test_skill_name_propagated_to_execution_log(self):
        from contracts.models import SkillDocument
        state = self._price_invocation_state(po_price=92.0)
        state.skill = SkillDocument(
            name="pricing-reconciliation",
            description="test",
            text="test skill text",
        )
        result = execute_recipe(state)
        assert result.execution_log.skill_name == "pricing-reconciliation"

    def test_shadow_verdict_propagated_to_execution_log(self):
        state = self._price_invocation_state(po_price=92.0)
        result = execute_recipe(state)
        assert result.execution_log.shadow_verdict == "GREEN"

    def test_skill_name_is_none_when_skill_not_loaded(self):
        state = self._price_invocation_state(po_price=92.0)
        state.skill = None
        result = execute_recipe(state)
        assert result.execution_log.skill_name is None

    def test_shadow_verdict_is_none_when_no_shadow(self):
        """Edge case: invocation present but shadow is None (should not happen in
        normal graph flow, but executor must not raise)."""
        state = self._price_invocation_state(po_price=92.0)
        state.shadow = None
        result = execute_recipe(state)
        assert result.execution_log.shadow_verdict is None


# ---------------------------------------------------------------------------
# DUPLICATE_PO — end-to-end node tests (TEST-1 fix)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# UNKNOWN intent handling (TEST-2 fix)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Recipe exception handling (TEST-3 fix)
# ---------------------------------------------------------------------------

class TestRecipeExceptionHandling:
    def test_recipe_exception_captured_in_execution_log(self):
        """If a recipe function raises, executor captures it as a structured error."""
        from recipes.executor import RecipeExecutor
        from constraints.specs import RecipeProposal

        proposal = RecipeProposal(recipe_name="PriceAdjustmentRecipe.py")
        # Pass erp_context without 'base_price' so recipe raises TypeError
        log = RecipeExecutor().run(
            proposal=proposal,
            params={
                "order_id": "SO-EXC01",
                "line_item": 1,
                "requested_price": 90.0,
                "erp_context": {},  # missing base_price → causes TypeError
            },
        )
        # RecipeExecutor wraps recipe calls in try/except and records the error.
        assert len(log.errors) > 0
        assert "Recipe raised" in log.errors[0]
        assert log.outputs == {}

    def test_missing_erp_context_key_causes_recipe_error(self):
        """Recipe with incomplete erp_context should not return SUCCESS."""
        from recipes.PriceAdjustmentRecipe import execute_price_correction
        try:
            result = execute_price_correction(
                order_id="SO-EXC02",
                line_item=1,
                requested_price=90.0,
                erp_context={},  # missing base_price, condition_type, max_discount_allowed
            )
            # If it returns, it should not be SUCCESS
            assert result.get("status") != "SUCCESS"
        except (TypeError, KeyError, ZeroDivisionError):
            # Expected: erp_context["base_price"] → KeyError
            pass


class TestUnknownIntentHandling:
    def test_unknown_intent_select_recipe_routes_to_fail_to_human(self):
        """UNKNOWN intent has no recipe mapping → FAIL_TO_HUMAN."""
        state = _state()
        state.intent = Intent.UNKNOWN
        result = select_recipe(state)
        assert result.final_status == TerminalStatus.FAIL_TO_HUMAN

    def test_unknown_intent_select_recipe_explanation_set(self):
        state = _state()
        state.intent = Intent.UNKNOWN
        result = select_recipe(state)
        assert result.explanation is not None
        assert "recipe" in result.explanation.lower() or "intent" in result.explanation.lower()

    def test_unknown_intent_no_recipe_selected(self):
        state = _state()
        state.intent = Intent.UNKNOWN
        result = select_recipe(state)
        assert result.selected_recipe is None


# ---------------------------------------------------------------------------
# DUPLICATE_PO — end-to-end node tests (TEST-1 fix)
# ---------------------------------------------------------------------------

class TestDuplicatePOClassifyNode:
    def test_duplicate_po_intent_classified(self, duplicate_po_event):
        result = classify(duplicate_po_event)
        assert result.intent == Intent.DUPLICATE_PO

    def test_duplicate_po_confidence_set(self, duplicate_po_event):
        result = classify(duplicate_po_event)
        assert 0.0 <= result.confidence <= 1.0


class TestDuplicatePOSelectRecipeNode:
    def test_duplicate_po_selects_recipe(self, duplicate_po_event):
        duplicate_po_event.intent = Intent.DUPLICATE_PO
        result = select_recipe(duplicate_po_event)
        assert result.selected_recipe == "DuplicatePORecipe.py"

    def test_duplicate_po_recipe_in_registry(self, duplicate_po_event):
        duplicate_po_event.intent = Intent.DUPLICATE_PO
        result = select_recipe(duplicate_po_event)
        from recipes.registry import REGISTRY
        assert result.selected_recipe in REGISTRY


class TestDuplicatePOShadowNode:
    def test_duplicate_po_shadow_is_green(self, duplicate_po_event):
        duplicate_po_event.intent = Intent.DUPLICATE_PO
        result = shadow_audit(duplicate_po_event)
        assert result.shadow is not None
        assert result.shadow.status == ShadowStatus.GREEN
        assert result.final_status is None


class TestDuplicatePOValidateTypesNode:
    def test_duplicate_po_builds_invocation(self, duplicate_po_event):
        duplicate_po_event.selected_recipe = "DuplicatePORecipe.py"
        result = validate_types(duplicate_po_event)
        assert result.invocation is not None
        assert result.invocation.recipe_name == "DuplicatePORecipe.py"

    def test_duplicate_po_invocation_contains_required_params(self, duplicate_po_event):
        duplicate_po_event.selected_recipe = "DuplicatePORecipe.py"
        result = validate_types(duplicate_po_event)
        for param in ("incoming_po_number", "customer_id", "signal_scores"):
            assert param in result.invocation.params

    def test_duplicate_po_signal_scores_from_metadata(self, duplicate_po_event):
        duplicate_po_event.selected_recipe = "DuplicatePORecipe.py"
        result = validate_types(duplicate_po_event)
        scores = result.invocation.params["signal_scores"]
        assert isinstance(scores, dict)
        assert scores.get("po_number") == 1.0

    def test_duplicate_po_resolution_context_from_resolved_data(self, duplicate_po_event):
        """Gateway-resolved context flows into recipe params."""
        duplicate_po_event.selected_recipe = "DuplicatePORecipe.py"
        duplicate_po_event.resolved_data = {
            "fulfillment_status": {"fulfilled": True},
            "matched_po_details": {"has_revision_indicator": True, "line_items_identical": False},
        }
        result = validate_types(duplicate_po_event)
        assert result.invocation.params["original_fulfilled"] is True
        assert result.invocation.params["has_revision_indicator"] is True
        assert result.invocation.params["line_items_identical"] is False

    def test_duplicate_po_resolution_context_defaults_when_no_gateway_data(self, duplicate_po_event):
        """Without gateway data, resolution context params are None."""
        duplicate_po_event.selected_recipe = "DuplicatePORecipe.py"
        result = validate_types(duplicate_po_event)
        assert result.invocation.params["original_fulfilled"] is None
        assert result.invocation.params["has_revision_indicator"] is None
        assert result.invocation.params["line_items_identical"] is None


class TestDuplicatePOExecuteRecipeNode:
    def test_duplicate_po_auto_block(self, duplicate_po_event):
        """High signal scores → AUTO_BLOCK → BLOCKED terminal status."""
        duplicate_po_event.intent = Intent.DUPLICATE_PO
        duplicate_po_event.shadow = _green_shadow()
        duplicate_po_event.invocation = RecipeInvocation(
            recipe_name="DuplicatePORecipe.py",
            params={
                "incoming_po_number": "PO-4001",
                "customer_id": "R-10",
                "signal_scores": {
                    "po_number": 1.0,
                    "customer_id": 1.0,
                    "line_items": 0.95,
                    "amount": 0.90,
                    "timestamp": 0.80,
                    "ship_to": 0.80,
                    "channel": 1.0,
                    "delivery_date": 0.80,
                },
                "threshold_auto_block": 0.90,
                "threshold_review_required": 0.70,
                "threshold_soft_flag": 0.50,
            },
        )
        result = execute_recipe(duplicate_po_event)
        assert result.execution_log is not None
        assert result.execution_log.outputs.get("classification") == "AUTO_BLOCK"
        assert result.final_status == TerminalStatus.BLOCKED

    def test_duplicate_po_pass(self):
        """Low signal scores → PASS → COMPLETE terminal status."""
        state = GraphState(event=OrderEvent(
            order_id="PO-4002",
            po_price=100.0,
            sap_base_price=100.0,
            event_type="EDI_850_DUPLICATE_PO",
            retailer_id="R-10",
            metadata={"signal_scores": {}},
        ))
        state.intent = Intent.DUPLICATE_PO
        state.shadow = _green_shadow()
        state.invocation = RecipeInvocation(
            recipe_name="DuplicatePORecipe.py",
            params={
                "incoming_po_number": "PO-4002",
                "customer_id": "R-10",
                "signal_scores": {},
                "threshold_auto_block": 0.90,
                "threshold_review_required": 0.70,
                "threshold_soft_flag": 0.50,
            },
        )
        result = execute_recipe(state)
        assert result.execution_log is not None
        assert result.execution_log.outputs.get("classification") == "PASS"
        assert result.final_status == TerminalStatus.COMPLETE
