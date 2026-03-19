from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from contracts.models import (
    GraphState,
    OrderEvent,
    TerminalStatus,
    WorkflowDefinition,
    WorkflowResult,
    WorkflowStepResult,
)

logger = logging.getLogger("asoe.workflows")


class WorkflowRunner:
    """Execute multi-step workflows using the Saga pattern.

    Each step runs through the full graph (intent classification →
    Compliance Shadow → recipe execution).  If step N fails after
    steps 1..N-1 succeeded, compensation recipes are invoked in
    reverse (LIFO) order for completed steps.

    Design references:
      - Garcia-Molina & Salem, "Sagas" (1987)
      - Pat Helland, "Life beyond Distributed Transactions"
      - CLAUDE.md §4: Compliance Shadow is mandatory (per step)
      - CLAUDE.md §5: Explicit failure is correct behavior

    Usage::

        runner = WorkflowRunner()
        result = runner.run(
            definition=WorkflowDefinition(
                workflow_id="WF-001",
                name="Duplicate Check then Price Adjust",
                steps=[
                    WorkflowStep(step_id="s1", intent=Intent.DUPLICATE_PO, ...),
                    WorkflowStep(step_id="s2", intent=Intent.CONTRACTUAL_CORRECTION, ...),
                ],
            ),
            events=[event_for_step_1, event_for_step_2],
        )
    """

    def run(
        self,
        definition: WorkflowDefinition,
        events: List[OrderEvent],
    ) -> WorkflowResult:
        """Execute all workflow steps sequentially.

        Args:
            definition: Workflow with ordered steps.
            events:     One OrderEvent per step.

        Returns:
            WorkflowResult with per-step results and any compensation log.
        """
        from orchestration.graph import run_graph

        step_results: List[WorkflowStepResult] = []
        completed: List[Tuple[str, Optional[str]]] = []  # (step_id, compensation_recipe)

        for i, step in enumerate(definition.steps):
            # Guard: need an event for every step
            if i >= len(events):
                logger.warning("workflow_missing_event step=%s", step.step_id)
                return WorkflowResult(
                    workflow_id=definition.workflow_id,
                    workflow_name=definition.name,
                    status="FAILED",
                    step_results=step_results,
                    compensation_log=self._compensate(completed)
                    + [f"Missing event for step {step.step_id}"],
                )

            # Build fresh GraphState from event
            state = GraphState(event=events[i])

            # Apply input mapping — carry state forward from previous step
            if step.input_mapping and step_results:
                prev = step_results[-1]
                if prev.execution_log is not None:
                    for target_key, source_field in step.input_mapping.items():
                        value = prev.execution_log.outputs.get(source_field)
                        if value is not None:
                            state.event.metadata[target_key] = value

            # Run through the full deterministic graph
            logger.info(
                "workflow_step_start step=%s intent=%s",
                step.step_id,
                step.intent.value,
            )
            result_state = run_graph(state)

            step_result = WorkflowStepResult(
                step_id=step.step_id,
                intent=result_state.intent,
                final_status=result_state.final_status or TerminalStatus.FAIL_TO_HUMAN,
                execution_log=result_state.execution_log,
                shadow_verdict=(
                    result_state.shadow.status.value if result_state.shadow else None
                ),
                explanation=result_state.explanation,
            )
            step_results.append(step_result)

            if result_state.final_status == TerminalStatus.COMPLETE:
                completed.append((step.step_id, step.compensation_recipe))
                logger.info("workflow_step_complete step=%s", step.step_id)
            else:
                # Step failed — compensate completed steps in reverse
                logger.warning(
                    "workflow_step_failed step=%s status=%s",
                    step.step_id,
                    result_state.final_status,
                )
                comp_log = self._compensate(completed)
                has_compensation = any(
                    s[1] is not None for s in completed
                )
                return WorkflowResult(
                    workflow_id=definition.workflow_id,
                    workflow_name=definition.name,
                    status="COMPENSATED" if has_compensation else "FAILED",
                    step_results=step_results,
                    compensation_log=comp_log,
                )

        logger.info("workflow_complete id=%s", definition.workflow_id)
        return WorkflowResult(
            workflow_id=definition.workflow_id,
            workflow_name=definition.name,
            status="COMPLETE",
            step_results=step_results,
        )

    def _compensate(
        self,
        completed: List[Tuple[str, Optional[str]]],
    ) -> List[str]:
        """Log compensation requirements in reverse (LIFO) order.

        Compensation recipes are declared, not executed inline — per
        CLAUDE.md §1: "If required execution logic does not exist as a
        recipe, stop and request a new recipe."

        In production, each compensation entry would be dispatched to
        the recipe executor.  For now the runner records the requirement
        so the caller or a human operator can act on it.
        """
        log: List[str] = []
        for step_id, comp_recipe in reversed(completed):
            if comp_recipe:
                log.append(
                    f"COMPENSATE step={step_id} recipe={comp_recipe}"
                )
            else:
                log.append(
                    f"NO_COMPENSATION step={step_id} (no compensation recipe declared)"
                )
        return log
