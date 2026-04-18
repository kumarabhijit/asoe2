"""GET /api/v1/health — Health check (architecture_v3.md Section 8.2).

Public endpoint (no auth required). Returns system status, version,
hardening switch states, and dynamic enum values per V1 Foundation
Guardrail #2 (architecture_v3.md Section 15).
"""

from __future__ import annotations

from fastapi import APIRouter

from api.schemas import HealthResponse
from constraints.specs import AllowedIntent, AllowedRecipeName, AllowedResolutionAction
from contracts.models import LIFECYCLE_STATES
from hardening.explain_mode import is_explain_mode_active
from hardening.kill_switch import is_kill_switch_active

router = APIRouter()

# Dynamic enum extraction from Pydantic Literal types
_ALLOWED_INTENTS = list(AllowedIntent.__args__)  # type: ignore[attr-defined]
_ALLOWED_RECIPES = list(AllowedRecipeName.__args__)  # type: ignore[attr-defined]
_ALLOWED_RESOLUTION_ACTIONS = list(AllowedResolutionAction.__args__)  # type: ignore[attr-defined]


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version="0.3.2",
        kill_switch=is_kill_switch_active(),
        explain_mode=is_explain_mode_active(),
        allowed_intents=_ALLOWED_INTENTS,
        lifecycle_states=LIFECYCLE_STATES,
        allowed_recipes=_ALLOWED_RECIPES,
        allowed_resolution_actions=_ALLOWED_RESOLUTION_ACTIONS,
    )
