"""Tests for the workflows package — WorkflowRunner with Saga pattern."""
from __future__ import annotations

import pytest

from contracts.models import (
    GraphState,
    Intent,
    OrderEvent,
    TerminalStatus,
    WorkflowDefinition,
    WorkflowResult,
    WorkflowStep,
    WorkflowStepResult,
)

langgraph = pytest.importorskip("langgraph")

from workflows.runner import WorkflowRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pricing_event(order_id: str = "SO-1001") -> OrderEvent:
    return OrderEvent(
        order_id=order_id,
        line_item=1,
        po_price=90.0,
        sap_base_price=100.0,
        retailer_id="R-01",
        line_count=1,
    )


def _mass_event(order_id: str = "SO-3001") -> OrderEvent:
    """Event that triggers MASS_PRICING_ERROR → RED shadow → BLOCKED."""
    return OrderEvent(
        order_id=order_id,
        line_item=1,
        po_price=70.0,
        sap_base_price=100.0,
        line_count=11,
    )


def _two_step_definition(
    compensation: str | None = None,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="WF-TEST-01",
        name="Two-step pricing workflow",
        steps=[
            WorkflowStep(
                step_id="s1",
                intent=Intent.CONTRACTUAL_CORRECTION,
                description="First price adjustment",
                compensation_recipe=compensation,
            ),
            WorkflowStep(
                step_id="s2",
                intent=Intent.CONTRACTUAL_CORRECTION,
                description="Second price adjustment",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# WorkflowRunner — success paths
# ---------------------------------------------------------------------------


class TestWorkflowRunnerSuccess:
    def test_all_steps_complete(self):
        runner = WorkflowRunner()
        result = runner.run(
            definition=_two_step_definition(),
            events=[_pricing_event("SO-A"), _pricing_event("SO-B")],
        )
        assert result.status == "COMPLETE"
        assert result.workflow_id == "WF-TEST-01"
        assert len(result.step_results) == 2
        assert all(
            sr.final_status == TerminalStatus.COMPLETE
            for sr in result.step_results
        )

    def test_step_results_carry_intent(self):
        runner = WorkflowRunner()
        result = runner.run(
            definition=_two_step_definition(),
            events=[_pricing_event(), _pricing_event()],
        )
        for sr in result.step_results:
            assert sr.intent == Intent.CONTRACTUAL_CORRECTION


# ---------------------------------------------------------------------------
# WorkflowRunner — failure and compensation
# ---------------------------------------------------------------------------


class TestWorkflowRunnerFailure:
    def test_step_failure_stops_workflow(self):
        """Step 1 succeeds, step 2 uses a mass event → RED → BLOCKED."""
        runner = WorkflowRunner()
        defn = _two_step_definition()
        result = runner.run(
            definition=defn,
            events=[_pricing_event(), _mass_event()],
        )
        # Step 2 failed, so workflow is FAILED (no compensation recipe)
        assert result.status == "FAILED"
        assert len(result.step_results) == 2
        assert result.step_results[0].final_status == TerminalStatus.COMPLETE
        assert result.step_results[1].final_status in (
            TerminalStatus.BLOCKED,
            TerminalStatus.FAIL_TO_HUMAN,
        )

    def test_compensation_logged_on_failure(self):
        """Step 1 has compensation recipe; step 2 fails → compensation logged."""
        runner = WorkflowRunner()
        defn = _two_step_definition(compensation="RollbackPriceRecipe.py")
        result = runner.run(
            definition=defn,
            events=[_pricing_event(), _mass_event()],
        )
        assert result.status == "COMPENSATED"
        assert any(
            "RollbackPriceRecipe.py" in entry
            for entry in result.compensation_log
        )

    def test_missing_events_fails_gracefully(self):
        runner = WorkflowRunner()
        defn = _two_step_definition()
        result = runner.run(
            definition=defn,
            events=[_pricing_event()],  # only 1 event for 2 steps
        )
        assert result.status == "FAILED"
        assert any("Missing event" in entry for entry in result.compensation_log)


# ---------------------------------------------------------------------------
# WorkflowRunner — input mapping
# ---------------------------------------------------------------------------


class TestWorkflowInputMapping:
    def test_state_carries_forward_via_mapping(self):
        """Step 2 receives data from step 1 output via input_mapping."""
        defn = WorkflowDefinition(
            workflow_id="WF-MAP-01",
            name="Mapped workflow",
            steps=[
                WorkflowStep(
                    step_id="s1",
                    intent=Intent.CONTRACTUAL_CORRECTION,
                    description="First",
                ),
                WorkflowStep(
                    step_id="s2",
                    intent=Intent.CONTRACTUAL_CORRECTION,
                    description="Second with mapping",
                    input_mapping={"prev_condition": "applied_condition"},
                ),
            ],
        )
        runner = WorkflowRunner()
        result = runner.run(
            definition=defn,
            events=[_pricing_event("SO-M1"), _pricing_event("SO-M2")],
        )
        # Both steps should complete; the mapping doesn't break execution
        assert result.status == "COMPLETE"
        assert len(result.step_results) == 2


# ---------------------------------------------------------------------------
# Contract models
# ---------------------------------------------------------------------------


class TestWorkflowContracts:
    def test_workflow_definition_extra_forbid(self):
        with pytest.raises(Exception):
            WorkflowDefinition(
                workflow_id="x", name="x", steps=[], unknown="y",
            )

    def test_workflow_result_status_literals(self):
        for status in ("COMPLETE", "FAILED", "COMPENSATED", "PARTIAL"):
            wr = WorkflowResult(
                workflow_id="x", workflow_name="x",
                status=status, step_results=[],
            )
            assert wr.status == status

    def test_step_result_fields(self):
        sr = WorkflowStepResult(
            step_id="s1",
            intent=Intent.CONTRACTUAL_CORRECTION,
            final_status=TerminalStatus.COMPLETE,
        )
        assert sr.shadow_verdict is None
        assert sr.execution_log is None
