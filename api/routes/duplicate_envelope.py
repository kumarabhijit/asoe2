"""ADR-028 G2 — canonical intent-specific envelope endpoints.

Single-round-trip views for specific intents. The UI consumes these
in place of {GET /exceptions/{id}, GET /exceptions/{id}/analysis,
GET /exceptions/{id}/trace, GET audit-log filter} — one fetch, one
source of truth, no client-side stitching (Verdict 2026-04-22
Pillar 2 — composer is the sole assembler).

Why a separate routes module?

  * The endpoints are scoped to single intents (DUPLICATE_PO today;
    BACK_ORDER / EDI_MISMATCH likely follow once their composers
    land). Co-locating them keeps the surface reviewable as one
    cohesive piece rather than scattered through the 1700-line
    `routes/exceptions.py`.
  * The path scheme is parallel to `/exceptions/{id}/<verb>` —
    `/exceptions/duplicates/{id}`, `/exceptions/back-orders/{id}` —
    so each new intent envelope drops in here without touching the
    generic exception routes.

Currently exposes:

  * GET /api/v1/exceptions/duplicates/{id}  (ADR-028 G2 — A6)

Mounted in `api/app.py` alongside the other route routers.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import get_tenant_id, require_role
from api.duplicate_envelope import (
    DuplicatePOEnvelope,
    compose_duplicate_po_envelope,
)
from api.errors import ASOEError
from api.store import exception_store


router = APIRouter()


# ---------------------------------------------------------------------------
# GET /api/v1/exceptions/duplicates/{id} — ADR-028 G2 canonical envelope
# ---------------------------------------------------------------------------
#
# Path is `/exceptions/duplicates/{id}` (NOT `/exceptions/{id}/duplicate`)
# so future intent-specific envelopes mount at parallel paths
# (`/exceptions/back-orders/{id}`, etc.) without colliding with the
# generic `/{id}/<verb>` routes.
#
# Status codes:
#   * 200 — envelope composed cleanly.
#   * 404 — record not found OR record exists but intent != DUPLICATE_PO.
#           The latter routes the caller back to the generic
#           /exceptions/{id} + /analysis endpoints rather than serving
#           a partial-truth envelope.
#   * 409 — record IS DUPLICATE_PO but recipe output is incomplete
#           (e.g., halted before recipe ran). Surfaces the precise
#           reason rather than synthesising a malformed envelope.


@router.get(
    "/exceptions/duplicates/{exception_id}",
    response_model=DuplicatePOEnvelope,
    dependencies=[Depends(require_role("analyst", "manager", "admin"))],
)
async def get_duplicate_po_envelope(
    exception_id: str,
    tenant_id: str = Depends(get_tenant_id),
) -> DuplicatePOEnvelope:
    """ADR-028 Guard-rail 2 — canonical DUPLICATE_PO envelope."""
    record = exception_store.get(exception_id, tenant_id)
    if record is None:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"Exception {exception_id} not found.",
            status_code=404,
        )
    if record.intent != "DUPLICATE_PO":
        raise ASOEError(
            code="WRONG_INTENT",
            message=(
                f"Exception {exception_id} is not a DUPLICATE_PO record "
                f"(intent={record.intent!r}). Use the generic "
                f"/exceptions/{{id}}/analysis endpoint instead."
            ),
            status_code=404,
        )
    audit_log = exception_store.get_audit_log(tenant_id)
    trace_data = exception_store.get_trace(exception_id)
    envelope = compose_duplicate_po_envelope(
        record=record,
        audit_log_entries=audit_log,
        trace_record=trace_data,
    )
    if envelope is None:
        # Composer returned None despite intent matching — recipe
        # output is malformed (missing required keys). Surface the
        # specific reason so the operator / SRE knows where the gap
        # is rather than serving a partial-truth response.
        raise ASOEError(
            code="ENVELOPE_INCOMPLETE",
            message=(
                f"Cannot compose duplicate-PO envelope for {exception_id}: "
                f"required recipe-output keys (signal_breakdown, "
                f"composite_score, classification, recommended_action) are "
                f"absent on resolution_data. Record may have halted before "
                f"the recipe ran (FAIL_TO_HUMAN, BLOCKED, etc.)."
            ),
            status_code=409,
        )
    return envelope
