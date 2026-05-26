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


# ──────────────────────────────────────────────────────────────────────────
# PARITY-0.5 — erasure certificate endpoint.
# A regulator (or a disputing customer) can demand proof that a customer
# document was deleted. The certificate returns the PII-free tombstone
# alongside the hash-chained audit-event reference; the chain itself
# (ADR-023) is the tamper-evident proof, not a wet signature.
# ──────────────────────────────────────────────────────────────────────────

@router.get(
    "/attachments/{attachment_id}/erasure-certificate",
    dependencies=[Depends(require_role("manager", "admin"))],
)
async def attachment_erasure_certificate(
    attachment_id: str,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Return the tombstone + audit-chain proof for an erased attachment.

    The PII-free tombstone is fetched from the tenant's audit log (the
    `ATTACHMENT_ERASED` event the erase routed there in PARITY-0.5).
    Tenant-scoped: a tenant can never read another tenant's certificate.
    404 when no erasure has been logged for the attachment id.
    """
    from api.store import exception_store

    log = exception_store.get_audit_log(tenant_id)
    event = next(
        (
            e for e in reversed(log)  # most recent erasure wins (idempotent)
            if e.get("policy_key") == "ATTACHMENT_ERASED"
            and (e.get("new_value") or {}).get("attachment_id") == attachment_id
        ),
        None,
    )
    if event is None:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"No erasure certificate found for attachment {attachment_id}.",
            status_code=404,
        )
    chain_ok, _break_idx = exception_store.verify_audit_chain(tenant_id)
    tombstone = event.get("new_value") or {}
    # Review 3 finding: a regulator-facing read should leave an
    # operational trail. INFO so App Insights / Log Analytics captures
    # who fetched the certificate and when. Phase 8 adds a stricter
    # CERTIFICATE_FETCHED chain event if regulator policy mandates it.
    logger.info(
        "erasure_certificate_fetched tenant=%s attachment_id=%s actor=%s "
        "audit_event_id=%s chain_verified=%s",
        tenant_id, attachment_id, getattr(user, "sub", "unknown"),
        event.get("id"), chain_ok,
    )
    return {
        "attachment_id": attachment_id,
        "tenant_id": tenant_id,
        "tombstone": tombstone,
        "audit_event": {
            "event_id": event.get("id"),
            "policy_key": event.get("policy_key"),
            "event_hash": event.get("event_hash"),
            "prev_hash": event.get("prev_hash"),
            "created_at": event.get("created_at"),
            "changed_by": event.get("changed_by"),
            "change_reason": event.get("change_reason"),
        },
        "chain_verified": chain_ok,
    }
