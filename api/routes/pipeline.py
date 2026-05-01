"""GET /api/v1/pipeline/topology — pipeline topology surface (ADR-027 Phase A).

Authenticated read-only endpoint that returns the compiled-graph
topology + A.0 verdict labels. The topology is the graph SHAPE — the
same nodes/edges for every authenticated user — so it carries no
per-tenant data; per-record evidence (executed path, decisions,
policy hits) lives on `/api/v1/exceptions/{id}/trace` and is gated
by the existing per-record retailer_id scoping.

Cached by `topology_hash` on the client; revalidates on
`useHealth` polling tick (ADR-027 Open Question §1).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import get_current_user
from api.schemas import PipelineTopology
from orchestration.graph import get_pipeline_topology

router = APIRouter()


@router.get("/pipeline/topology", response_model=PipelineTopology)
async def get_topology(_user=Depends(get_current_user)) -> PipelineTopology:
    """Return the live-graph topology + per-gate verdict labels.

    Authentication required; no additional permission. ADR-027 §
    "Backend changes" #2: adding `dashboard:read` here would be
    permission theater (no per-tenant data to leak).
    """
    return get_pipeline_topology()
