"""ADR-041 P3e Phase 3 sign-off gate #5 — cases V2 telemetry receivers.

Three best-effort metric endpoints the asoe-ui V2 surfaces emit
to. Each is:

  * authenticated (analyst+ — reviewer actions, not background noise)
  * `include_in_schema=False` (operational telemetry, not a public
    API surface)
  * `202 Accepted` (asynchronous record; never blocks the UI)
  * audit_bearing-decorated so the rolling audit lineage captures
    the operator-decision context the metric correlates with

Wire shape mirrors the asoe-ui reporters in
`src/lib/telemetry.ts`. Unknown / malformed fields validate at
the Pydantic boundary — the UI's best-effort contract treats any
4xx as a benign drop.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from api.deps import require_role
from api.metrics import (
    record_cases_analysis_scroll_depth,
    record_cases_row_click,
    record_cases_time_to_first_action,
)
from api.observability.audit_bearing import audit_bearing


router = APIRouter(prefix="/api/v1/metrics/cases", tags=["metrics"])


# ---------------------------------------------------------------------------
# POST /api/v1/metrics/cases/row-click
# ---------------------------------------------------------------------------


class RowClickRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(min_length=1)
    row_index: int | None = Field(default=None, ge=0)
    audit_verdict_color: str | None = Field(
        default=None,
        # Constrained to R/A/G or null per the asoe-ui mirror; any
        # other value gets coerced to null at the recorder level
        # (the metric stores it under "none").
        pattern=r"^[RAG]$",
    )


@router.post(
    "/row-click",
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=False,
    dependencies=[Depends(require_role("analyst", "manager", "admin"))],
)
@audit_bearing(
    reason="records one queue-row click for ADR-041 P3e Phase 3 "
           "gate #5 (row-click rate) — feeds the SLA-sort "
           "effectiveness story.",
)
async def row_click(req: RowClickRequest) -> dict:
    record_cases_row_click(verdict=req.audit_verdict_color)
    return {"ok": True}


# ---------------------------------------------------------------------------
# POST /api/v1/metrics/cases/time-to-first-action
# ---------------------------------------------------------------------------


class TimeToFirstActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(min_length=1)
    dwell_ms: float = Field(ge=0)
    first_action: str = Field(
        pattern=r"^(approve|reject|override|escalate|reanalyze)$",
    )


@router.post(
    "/time-to-first-action",
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=False,
    dependencies=[Depends(require_role("analyst", "manager", "admin"))],
)
@audit_bearing(
    reason="records the dwell from case-open to first HITL action "
           "for ADR-041 P3e Phase 3 gate #5 — the V2 vs legacy "
           "comparison surface for the CSA dry-run.",
)
async def time_to_first_action(req: TimeToFirstActionRequest) -> dict:
    record_cases_time_to_first_action(
        dwell_ms=req.dwell_ms,
        first_action=req.first_action,
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# POST /api/v1/metrics/cases/analysis-scroll-depth
# ---------------------------------------------------------------------------


class AnalysisScrollDepthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(min_length=1)
    max_scroll_pct: float = Field(ge=0.0, le=1.0)
    acted: bool


@router.post(
    "/analysis-scroll-depth",
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=False,
    dependencies=[Depends(require_role("analyst", "manager", "admin"))],
)
@audit_bearing(
    reason="records how far operators read into Agent Analysis "
           "before clicking — the Compliance audit-format review "
           "sub-gate for ADR-041 P3e Phase 3.",
)
async def analysis_scroll_depth(req: AnalysisScrollDepthRequest) -> dict:
    record_cases_analysis_scroll_depth(
        max_scroll_pct=req.max_scroll_pct,
        acted=req.acted,
    )
    return {"ok": True}
