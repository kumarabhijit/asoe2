"""GET /api/v1/health — Health check (architecture_v3.md Section 8.2).

Public endpoint (no auth required). Returns system status, version,
hardening switch states, and dynamic enum values per V1 Foundation
Guardrail #2 (architecture_v3.md Section 15).
"""

from __future__ import annotations

from fastapi import APIRouter

from api.schemas import AutonomyLevelInfo, HealthResponse
from contracts.autonomy import (
    CURRENT_AUTONOMY_VOCAB_VERSION,
    allowed_autonomy_levels,
)
from constraints.specs import (
    INTENT_REASON_TAGS,
    AllowedIntent,
    AllowedOverrideReasonTag,
    AllowedRecipeName,
    AllowedResolutionAction,
)
from contracts.models import CaseSource, CaseStatus, LIFECYCLE_STATES
from hardening.explain_mode import is_explain_mode_active
from hardening.kill_switch import is_kill_switch_active

router = APIRouter()

# Dynamic enum extraction from Pydantic Literal types
_ALLOWED_INTENTS = list(AllowedIntent.__args__)  # type: ignore[attr-defined]
_ALLOWED_RECIPES = list(AllowedRecipeName.__args__)  # type: ignore[attr-defined]
_ALLOWED_RESOLUTION_ACTIONS = list(AllowedResolutionAction.__args__)  # type: ignore[attr-defined]
_ALLOWED_OVERRIDE_REASON_TAGS = list(AllowedOverrideReasonTag.__args__)  # type: ignore[attr-defined]
_REASON_TAGS_BY_INTENT = {k: list(v) for k, v in INTENT_REASON_TAGS.items()}
# Phase 28.5.x §D1 — case-level vocabularies sourced from the same
# contracts/models.py Literal definitions so a backend rename surfaces
# in the UI Guardrail #1 lock before it silently empties a chip group.
_ALLOWED_CASE_STATUSES = list(CaseStatus.__args__)  # type: ignore[attr-defined]
_ALLOWED_CASE_SOURCES = list(CaseSource.__args__)  # type: ignore[attr-defined]


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
        allowed_override_reason_tags=_ALLOWED_OVERRIDE_REASON_TAGS,
        allowed_override_reason_tags_by_intent=_REASON_TAGS_BY_INTENT,
        allowed_case_statuses=_ALLOWED_CASE_STATUSES,
        allowed_case_sources=_ALLOWED_CASE_SOURCES,
        autonomy_vocab_version=CURRENT_AUTONOMY_VOCAB_VERSION,
        allowed_autonomy_levels=[
            AutonomyLevelInfo(**entry) for entry in allowed_autonomy_levels()
        ],
    )


# ---------------------------------------------------------------------------
# GET /api/v1/metrics — Prometheus SLI surface (ADR-039 §7.3)
# ---------------------------------------------------------------------------
#
# Public endpoint (no auth) so the in-cluster Prometheus
# scraper can read it without managing a service-account
# credential. The response shape is the standard Prometheus
# text exposition format; see api/metrics.py for the metric
# families emitted.
#
# Operators wire the scrape config via the existing
# ServiceMonitor pattern; the metric namespace is
# `shadow_llm_*` so the dashboard import is a one-step.

from fastapi import Response  # noqa: E402

from api.metrics import render_all as _render_metrics  # noqa: E402


@router.get(
    "/metrics",
    response_class=Response,
    include_in_schema=False,  # Prometheus scrape, not part of the UI contract
)
async def metrics() -> Response:
    """Emit the ADR-039 §7.3 Prometheus SLI surface.

    Content-Type per the Prometheus text-exposition spec:
    `text/plain; version=0.0.4; charset=utf-8`. Scrapers expect
    that exact value; do not change it.
    """
    return Response(
        content=_render_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# POST /api/v1/metrics/reviewer-activity — automation-bias telemetry (DoR #11)
# ---------------------------------------------------------------------------
# The UI reports one event per operator decision: how long the operator dwelled
# on the case and whether they expanded Layer-2 evidence first. Authenticated
# (a reviewer action), but never blocks the UI — best-effort telemetry.

from fastapi import Depends, status  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

from api.deps import require_role  # noqa: E402
from api.metrics import record_reviewer_activity  # noqa: E402


class ReviewerActivityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dwell_ms: float = Field(ge=0)
    layer2_opened: bool


@router.post(
    "/metrics/reviewer-activity",
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=False,
    dependencies=[Depends(require_role("analyst", "manager", "admin"))],
)
async def reviewer_activity(req: ReviewerActivityRequest) -> dict:
    """Record one decision's automation-bias signals (Layer-2-open + dwell)."""
    record_reviewer_activity(dwell_ms=req.dwell_ms, layer2_opened=req.layer2_opened)
    return {"ok": True}
