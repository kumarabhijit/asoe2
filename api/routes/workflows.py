"""POST /api/v1/workflows — Multi-step workflow execution (architecture_v3.md Section 8.2).

Accepts a WorkflowDefinition + base event, runs through WorkflowRunner,
returns WorkflowResult.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import get_tenant_id, require_role
from api.errors import ASOEError
from api.schemas import WorkflowRequest
from contracts.models import (
    Intent,
    OrderEvent,
    WorkflowDefinition,
    WorkflowStep,
)

router = APIRouter()


@router.post(
    "/workflows",
    dependencies=[Depends(require_role("manager", "admin"))],
)
async def run_workflow(
    req: WorkflowRequest,
    tenant_id: str = Depends(get_tenant_id),
) -> dict:
    from workflows.runner import WorkflowRunner

    # Build base event from request (ResolveRequest inherits from OrderEvent)
    event = OrderEvent.model_validate(req.base_event.model_dump())

    # Build workflow definition
    steps = []
    for s in req.steps:
        try:
            intent = Intent(s.intent)
        except ValueError:
            raise ASOEError(
                code="INVALID_INTENT",
                message=f"Unknown intent: {s.intent}",
                status_code=400,
            )
        steps.append(
            WorkflowStep(
                step_id=s.step_id,
                intent=intent,
                description=s.description,
                input_mapping=s.input_mapping,
                compensation_recipe=s.compensation_recipe,
            )
        )

    definition = WorkflowDefinition(
        workflow_id=req.workflow_id,
        name=req.name,
        steps=steps,
    )

    # WorkflowRunner expects one OrderEvent per step. The base event is
    # cloned for each step (input_mapping carries state between steps).
    events = [event] * len(steps)

    runner = WorkflowRunner()
    result = runner.run(definition, events)
    return result.model_dump()
