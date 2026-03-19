from __future__ import annotations

import pytest
from pydantic import ValidationError

from contracts.models import (
    ComplianceDecision,
    ExecutionLog,
    GraphState,
    Intent,
    OrderEvent,
    PricingDiscrepancy,
    RecipeInvocation,
    ShadowStatus,
    TerminalStatus,
)


# ---------------------------------------------------------------------------
# Enum vocabulary
# ---------------------------------------------------------------------------

class TestEnums:
    def test_intent_values(self):
        assert set(Intent) == {
            Intent.CONTRACTUAL_CORRECTION,
            Intent.CREDIT_BLOCK,
            Intent.MASS_PRICING_ERROR,
            Intent.DUPLICATE_PO,
            Intent.UNKNOWN,
        }

    def test_shadow_status_values(self):
        assert set(ShadowStatus) == {ShadowStatus.GREEN, ShadowStatus.YELLOW, ShadowStatus.RED}

    def test_terminal_status_values(self):
        assert set(TerminalStatus) == {
            TerminalStatus.COMPLETE,
            TerminalStatus.FAIL_TO_HUMAN,
            TerminalStatus.MANUAL_REVIEW_REQUIRED,
            TerminalStatus.BLOCKED,
            TerminalStatus.REJECTED,
        }


# ---------------------------------------------------------------------------
# OrderEvent
# ---------------------------------------------------------------------------

class TestOrderEvent:
    def test_minimal_valid_event(self):
        ev = OrderEvent(order_id="SO-1", po_price=90.0, sap_base_price=100.0)
        assert ev.order_id == "SO-1"
        assert ev.line_count == 1

    def test_extra_fields_are_forbidden(self):
        with pytest.raises(ValidationError):
            OrderEvent(order_id="SO-1", po_price=90.0, sap_base_price=100.0, unknown_field="x")

    def test_missing_required_fields_raise(self):
        with pytest.raises(ValidationError):
            OrderEvent(order_id="SO-1")  # po_price and sap_base_price missing

    def test_optional_credit_fields_default_none(self):
        ev = OrderEvent(order_id="SO-1", po_price=90.0, sap_base_price=100.0)
        assert ev.credit_limit is None
        assert ev.current_exposure is None
        assert ev.requester_role is None


# ---------------------------------------------------------------------------
# PricingDiscrepancy
# ---------------------------------------------------------------------------

class TestPricingDiscrepancy:
    def test_valid_discrepancy(self):
        d = PricingDiscrepancy(delta=-10.0, delta_pct=-0.10, within_threshold=True)
        assert d.delta == -10.0

    def test_extra_fields_are_forbidden(self):
        with pytest.raises(ValidationError):
            PricingDiscrepancy(delta=1.0, delta_pct=0.01, within_threshold=True, extra="x")


# ---------------------------------------------------------------------------
# RecipeInvocation
# ---------------------------------------------------------------------------

class TestRecipeInvocation:
    def test_valid_invocation(self):
        inv = RecipeInvocation(recipe_name="PriceAdjustmentRecipe.py", params={"order_id": "SO-1"})
        assert inv.recipe_name == "PriceAdjustmentRecipe.py"

    def test_extra_fields_are_forbidden(self):
        with pytest.raises(ValidationError):
            RecipeInvocation(recipe_name="R", params={}, extra="x")


# ---------------------------------------------------------------------------
# ComplianceDecision
# ---------------------------------------------------------------------------

class TestComplianceDecision:
    def test_trace_id_auto_generated(self):
        d = ComplianceDecision(status=ShadowStatus.GREEN)
        assert d.trace_id and len(d.trace_id) > 0

    def test_two_decisions_have_distinct_trace_ids(self):
        a = ComplianceDecision(status=ShadowStatus.GREEN)
        b = ComplianceDecision(status=ShadowStatus.GREEN)
        assert a.trace_id != b.trace_id

    def test_reasons_and_policy_hits_default_empty(self):
        d = ComplianceDecision(status=ShadowStatus.YELLOW)
        assert d.reasons == []
        assert d.policy_hits == []

    def test_extra_fields_are_forbidden(self):
        with pytest.raises(ValidationError):
            ComplianceDecision(status=ShadowStatus.GREEN, unknown="x")


# ---------------------------------------------------------------------------
# ExecutionLog
# ---------------------------------------------------------------------------

class TestExecutionLog:
    def test_minimal_valid_log(self):
        log = ExecutionLog(trace_id="trace-001")
        assert log.trace_id == "trace-001"
        assert log.errors == []
        assert log.constrained_outputs == {}

    def test_extra_fields_are_forbidden(self):
        with pytest.raises(ValidationError):
            ExecutionLog(trace_id="t", extra="x")

    def test_skill_name_field_defaults_none(self):
        log = ExecutionLog(trace_id="trace-001")
        assert log.skill_name is None

    def test_shadow_verdict_field_defaults_none(self):
        log = ExecutionLog(trace_id="trace-001")
        assert log.shadow_verdict is None

    def test_skill_name_can_be_set(self):
        log = ExecutionLog(trace_id="trace-001", skill_name="pricing-reconciliation")
        assert log.skill_name == "pricing-reconciliation"

    def test_shadow_verdict_can_be_set(self):
        log = ExecutionLog(trace_id="trace-001", shadow_verdict="GREEN")
        assert log.shadow_verdict == "GREEN"


# ---------------------------------------------------------------------------
# GraphState
# ---------------------------------------------------------------------------

class TestGraphState:
    def test_default_intent_is_unknown(self, pricing_event):
        assert pricing_event.intent == Intent.UNKNOWN

    def test_default_confidence_is_zero(self, pricing_event):
        assert pricing_event.confidence == 0.0

    def test_explanation_auto_filled_for_terminal_status(self):
        state = GraphState(
            event=OrderEvent(order_id="SO-X", po_price=90.0, sap_base_price=100.0),
            final_status=TerminalStatus.BLOCKED,
        )
        assert state.explanation is not None
        assert "BLOCKED" in state.explanation

    def test_no_explanation_required_when_no_terminal_status(self):
        state = GraphState(
            event=OrderEvent(order_id="SO-X", po_price=90.0, sap_base_price=100.0),
        )
        assert state.explanation is None

    def test_extra_fields_are_forbidden(self):
        with pytest.raises(ValidationError):
            GraphState(
                event=OrderEvent(order_id="SO-X", po_price=90.0, sap_base_price=100.0),
                unknown_field="x",
            )

    def test_rag_context_defaults_to_empty(self, pricing_event):
        assert pricing_event.rag_context.chunks == []
        assert pricing_event.rag_context.metadata == {}
