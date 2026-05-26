"""Attachment download endpoint (ADR-042 / DoR #10).

GET /api/v1/cases/{case_id}/attachments/{attachment_id}
  Stream a stored email attachment's bytes to an authorised operator.

The attachment store (`gateways/attachment_store.py`) is tenant-scoped; this
read mirrors the case-detail RBAC (analyst / manager / admin / viewer / partner)
and serves an attachment only when it belongs to the caller's tenant AND the
path `case_id`, otherwise 404 — a cross-tenant or wrong-case id is
indistinguishable from "not found".

Served as `Content-Disposition: attachment` (forced download, never inline) +
`X-Content-Type-Options: nosniff` so untrusted file content cannot render/execute
in the browser (the XSS posture that pairs with the SSRF guard on the fetch side).
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Response

from api.attachment_read_token import (
    ReadTokenError,
    mint_read_token,
    verify_read_token,
)
from api.deps import (
    AuthenticatedUser,
    get_current_user,
    get_tenant_id,
    require_role,
)
from api.errors import ASOEError
from contracts.policy import ATTACHMENT_READ_URL_TTL_SECONDS
from gateways.attachment_store import get_attachment

logger = logging.getLogger("asoe.api.attachments")

router = APIRouter()


def _safe_filename(name: str) -> str:
    """Strip header-injection / quote characters from an operator-supplied
    filename before it lands in a Content-Disposition header."""
    cleaned = "".join(
        c for c in (name or "") if c.isprintable() and c not in '"\\\r\n'
    ).strip()
    return cleaned or "attachment"


def _attachment_response(record) -> Response:
    """Forced-download response for an attachment record (never inline)."""
    filename = _safe_filename(record.name)
    return Response(
        content=record.content,
        media_type=record.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/cases/{case_id}/attachments/{attachment_id}",
    dependencies=[Depends(require_role(
        "analyst", "manager", "admin", "viewer", "partner",
    ))],
)
async def download_attachment(
    case_id: str,
    attachment_id: str,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001
) -> Response:
    # The store read is tenant-scoped, so a cross-tenant id returns None.
    record = get_attachment(tenant_id, attachment_id)
    if record is None or record.case_id != case_id:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"Attachment {attachment_id} not found.",
            status_code=404,
        )
    return _attachment_response(record)


@router.post(
    "/cases/{case_id}/attachments/{attachment_id}/signed-url",
    dependencies=[Depends(require_role(
        "analyst", "manager", "admin", "viewer", "partner",
    ))],
)
async def create_signed_read_url(
    case_id: str,
    attachment_id: str,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001
) -> dict:
    """Mint a short-TTL, scoped capability URL for an attachment's bytes
    (ADR-044 §2.2). RBAC + tenant + case-scoped at mint time; the returned URL
    needs no auth header (the signed token is the capability) and expires."""
    record = get_attachment(tenant_id, attachment_id)
    if record is None or record.case_id != case_id:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"Attachment {attachment_id} not found.",
            status_code=404,
        )
    token = mint_read_token(
        tenant_id=tenant_id, case_id=case_id, attachment_id=attachment_id,
    )
    return {
        "url": f"/api/v1/attachments/read?token={token}",
        "expires_at": int(time.time()) + ATTACHMENT_READ_URL_TTL_SECONDS,
    }


@router.get("/attachments/read")
async def read_attachment_by_token(token: str) -> Response:
    """Stream attachment bytes for a valid scoped read token (ADR-044 §2.2).

    No Authorization header: the signed, expiring token is the capability. It is
    bound to a single (tenant, case, attachment) tuple, so it cannot read across
    tenants/cases, and is rejected once expired."""
    try:
        claims = verify_read_token(token)
    except ReadTokenError:
        raise ASOEError(
            code="FORBIDDEN", message="Invalid or expired read token.", status_code=403,
        )
    record = get_attachment(claims["t"], claims["a"])
    if record is None or record.case_id != claims["c"]:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"Attachment {claims['a']} not found.",
            status_code=404,
        )
    return _attachment_response(record)
