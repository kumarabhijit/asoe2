"""POST /api/v1/_sandbox/* — test-fixture endpoints for Playwright.

Gated twice: the router is mounted only when ASOE_ENV=sandbox, AND each
handler additionally asserts the env at call time (defense in depth, so
a mis-configured include_router() in prod can't accidentally expose
fixture surgery). Nothing here can create audit events that would
contaminate a production chain because triggers on
policy_audit_log reject any UPDATE/DELETE, and the seed endpoint only
INSERTs on a fresh tenant_id or updates resolution_data metadata on a
record the caller just created via /resolve or /resolve/explain.

Endpoints:
  POST /api/v1/_sandbox/seed/financial-impact
    Attach a `financial_impact_usd` value to an existing exception's
    resolution_data so the four-eyes /disposition path stages to
    PENDING_COSIGN. Required for the Playwright four-eyes cosign spec.

  POST /api/v1/_sandbox/tenant/reset
    Clear the in-memory exception store for a given tenant_id. Used by
    Playwright fixtures to isolate specs. In the DB-backed configuration
    this is a no-op (tests use SQLite :memory: per adapter spin-up, not
    this endpoint).

  POST /api/v1/_sandbox/seed/manual-order-intake
    Stand-in for the future email-intelligence-agent / EDI ingestion
    producer. Emits a fully-formed MANUAL_ORDER_INTAKE event through
    the normal /exceptions/resolve flow so sandbox fixtures (and the
    §28.6 dashboard's event_type_received_total gauge) observe the
    canonical post-rename event_type. The ASOE_EMIT_LEGACY_EVENT_TYPES=1
    env flag flips emission to the transitional
    EMAIL_ORDER_ENTRY_REQUEST name so the dual-acceptance contract
    (ADR-034 §6.2) can be exercised end-to-end without re-hardcoding
    the legacy literal in fixture code. Removed at the §6.2 deadline.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from api.deps import AuthenticatedUser, get_current_user, get_tenant_id, require_role
from api.store import exception_store

router = APIRouter()


# ADR-034 §6.2 — the canonical, post-rename event_type the producer emits
# by default. The legacy alias is kept for one release cycle behind the
# ASOE_EMIT_LEGACY_EVENT_TYPES flag so backward-compat suites can drive
# the dual-acceptance path without re-hardcoding the deprecated literal.
_CANONICAL_MANUAL_EVENT_TYPE = "MANUAL_ORDER_INTAKE"
_LEGACY_MANUAL_EVENT_TYPE = "EMAIL_ORDER_ENTRY_REQUEST"


def _emit_legacy_event_types() -> bool:
    """Producer-side legacy-name toggle (ADR-034 §6.2).

    Default off — the canonical name ships unless a backward-compat
    test opts in. The flag is read at call time so a test can flip it
    via monkeypatch without re-importing the module.
    """
    return os.getenv("ASOE_EMIT_LEGACY_EVENT_TYPES", "").strip().lower() in (
        "1", "true", "yes",
    )


def _require_sandbox() -> None:
    """Second defence against mis-include in prod."""
    if os.getenv("ASOE_ENV", "production").lower() != "sandbox":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sandbox endpoints are disabled outside ASOE_ENV=sandbox.",
        )


class SeedFinancialImpactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exception_id: str
    financial_impact_usd: float


@router.post("/_sandbox/seed/financial-impact", dependencies=[Depends(require_role("manager", "admin"))])
async def seed_financial_impact(
    req: SeedFinancialImpactRequest,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001
) -> dict:
    """Stamp financial_impact_usd onto an exception's resolution_data.

    Used by the Playwright four-eyes spec to push an existing record
    past HIGH_VALUE_OVERRIDE_THRESHOLD_USD so the next /disposition
    call stages to PENDING_COSIGN. No new audit event is emitted —
    this is test fixture wiring, not a SOX-relevant state transition.
    """
    _require_sandbox()
    rec = exception_store.get(req.exception_id, tenant_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="exception not found")
    merged = dict(rec.resolution_data or {})
    merged["financial_impact_usd"] = req.financial_impact_usd
    exception_store.update(req.exception_id, tenant_id, resolution_data=merged)
    return {
        "exception_id": req.exception_id,
        "financial_impact_usd": req.financial_impact_usd,
        "ok": True,
    }


class TenantResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Optional — defaults to the caller's tenant_id. Present for specs
    # that want to reset a specific cross-tenant chain.
    tenant_id: Optional[str] = None


@router.post("/_sandbox/tenant/reset", dependencies=[Depends(require_role("admin"))])
async def reset_tenant(
    req: TenantResetRequest,
    caller_tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001
) -> dict:
    """Clear in-memory exception records for a tenant.

    For the in-memory store (the default sandbox mode), this wipes all
    exceptions belonging to the target tenant_id so the next spec starts
    from a clean slate. For the DB-backed store, this is a no-op — tests
    against a real DB spin up their own SQLite :memory: per run.
    """
    _require_sandbox()
    target = req.tenant_id or caller_tenant_id
    # Guard: admins can only reset their own tenant's data — a
    # cross-tenant wipe would require infra-level access.
    if target != caller_tenant_id:
        raise HTTPException(
            status_code=403,
            detail="Admin can only reset their own tenant's sandbox data.",
        )
    removed = 0
    if hasattr(exception_store, "_records"):
        to_remove = [
            eid for eid, rec in exception_store._records.items()  # type: ignore[attr-defined]
            if rec.tenant_id == target
        ]
        for eid in to_remove:
            del exception_store._records[eid]  # type: ignore[attr-defined]
            removed += 1
    # Audit chain: also clear the in-memory audit log for this tenant
    # so verify_audit_chain() returns (True, None) from GENESIS for the
    # next spec's write sequence.
    if hasattr(exception_store, "_audit_log"):
        before = len(exception_store._audit_log)  # type: ignore[attr-defined]
        exception_store._audit_log = [  # type: ignore[attr-defined]
            e for e in exception_store._audit_log  # type: ignore[attr-defined]
            if e.get("tenant_id") != target
        ]
        removed += before - len(exception_store._audit_log)  # type: ignore[attr-defined]
    return {"tenant_id": target, "removed": removed, "ok": True}


class SeedManualOrderIntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Defaults are tuned to land a clean GREEN MANUAL_ORDER_INTAKE
    # event: high composite_confidence + all four non-disableable
    # floor checks True. Tests that want a specific lifecycle outcome
    # (REVIEW / FATAL_REJECT) override individual fields.
    order_id: Optional[str] = None
    composite_confidence: float = 0.97
    non_disableable_floor: Optional[dict] = None
    validation_failures: Optional[list[str]] = None
    reject_reason_code: Optional[str] = None
    metadata_extra: Optional[dict] = Field(
        default=None,
        description=(
            "Free-form metadata blob merged into the emitted event's "
            "metadata dict. The producer never overrides "
            "composite_confidence / non_disableable_floor with these "
            "keys — those have dedicated fields above."
        ),
    )


def _build_manual_intake_event_payload(
    req: SeedManualOrderIntakeRequest,
) -> dict[str, Any]:
    """Construct the inbound /exceptions/resolve payload.

    The event_type is selected at call time so the
    `ASOE_EMIT_LEGACY_EVENT_TYPES` toggle is exercised live (the test
    flips the env var between two calls and asserts the
    `event_type_received_total` cardinality moves accordingly).
    """
    event_type = (
        _LEGACY_MANUAL_EVENT_TYPE
        if _emit_legacy_event_types()
        else _CANONICAL_MANUAL_EVENT_TYPE
    )
    floor = req.non_disableable_floor or {
        "sender_authorized": True,
        "customer_resolved": True,
        "duplicate_po_clear": True,
        "credit_clear": True,
    }
    metadata: dict[str, Any] = {
        "composite_confidence": req.composite_confidence,
        "non_disableable_floor": floor,
        "validation_failures": req.validation_failures or [],
    }
    if req.reject_reason_code is not None:
        metadata["reject_reason_code"] = req.reject_reason_code
    if req.metadata_extra:
        # Producer-side metadata extension. The reserved keys above
        # cannot be overridden via metadata_extra to preserve the
        # confidence-band routing contract; sneaking them in would
        # divorce the ?confidence query from the routed band.
        for key, value in req.metadata_extra.items():
            if key in ("composite_confidence", "non_disableable_floor", "validation_failures"):
                continue
            metadata[key] = value
    return {
        "order_id": req.order_id or "EML-PO-SEED",
        "line_item": 1,
        "po_price": 100.0,
        "sap_base_price": 100.0,
        "event_type": event_type,
        "retailer_id": "acct-southeast-distrib",
        "line_count": 1,
        "metadata": metadata,
    }


@router.post(
    "/_sandbox/seed/manual-order-intake",
    dependencies=[Depends(require_role("analyst", "manager", "admin"))],
)
async def seed_manual_order_intake(
    req: SeedManualOrderIntakeRequest,
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001
) -> dict:
    """Producer-side stand-in for the email-intelligence-agent.

    Emits a MANUAL_ORDER_INTAKE event by default and routes it through
    the normal /exceptions/resolve graph so the dashboard's
    `event_type_received_total` gauge sees the canonical name.
    ASOE_EMIT_LEGACY_EVENT_TYPES=1 flips emission to the transitional
    EMAIL_ORDER_ENTRY_REQUEST literal for dual-acceptance soaks.

    Returns the resulting exception_id, the event_type that was actually
    emitted, and whether the legacy flag was honoured — the caller's
    backward-compat suite asserts on all three.
    """
    _require_sandbox()
    # Import locally — these touch the orchestration graph and a
    # module-level import would pull half the recipe surface into
    # sandbox.py at app boot.
    from api.routes.exceptions import (  # noqa: PLC0415
        _build_order_event,
        _persist_exception,
        _publish_task_complete,
        _resolve_state,
        _state_to_resolve_response,
    )
    from api.schemas import ResolveRequest  # noqa: PLC0415
    from contracts.models import GraphState  # noqa: PLC0415

    payload = _build_manual_intake_event_payload(req)
    emitted_event_type = payload["event_type"]
    emitted_legacy = emitted_event_type == _LEGACY_MANUAL_EVENT_TYPE

    resolve_req = ResolveRequest.model_validate(payload)
    event = _build_order_event(resolve_req)
    state = GraphState(event=event, tenant_id=tenant_id)
    try:
        final_state = _resolve_state(state, tenant_id)
    except Exception as exc:  # pragma: no cover - bubble up like /resolve
        raise HTTPException(status_code=500, detail=f"graph failure: {exc}")

    trace_id = final_state.shadow.trace_id if final_state.shadow else None
    exception_id = _persist_exception(tenant_id, final_state, trace_id)
    _publish_task_complete(tenant_id, exception_id, trace_id or "", final_state)
    response = _state_to_resolve_response(exception_id, final_state)
    return {
        "exception_id": exception_id,
        "event_type": emitted_event_type,
        "emitted_legacy": emitted_legacy,
        "final_status": response.final_status,
        "ok": True,
    }


class SeedCaseAttachmentRequest(BaseModel):
    """Sandbox attachment ingest. ``content_b64`` is optional — when omitted a
    deterministic sample blob is stored so the download path can be exercised
    without supplying bytes."""

    model_config = ConfigDict(extra="forbid")
    name: str = "sample.pdf"
    mime_type: str = "application/pdf"
    content_b64: Optional[str] = None


@router.post(
    "/_sandbox/cases/{case_id}/attachments",
    dependencies=[Depends(require_role("analyst", "manager", "admin"))],
)
async def seed_case_attachment(
    case_id: str,
    req: SeedCaseAttachmentRequest,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001
) -> dict:
    """Dev/demo producer for the attachment store (stand-in for the future
    email-intelligence-agent / ADR-036 ingestion).

    Persists an attachment's bytes under (tenant, case_id) so the production
    read path — GET /cases/{case_id}/attachments/{attachment_id} — can be
    exercised end-to-end. Sandbox-only; real ingestion lands with ADR-036.
    """
    _require_sandbox()
    # Local import — keeps the attachment store out of the app-boot import graph.
    from gateways.attachment_store import AttachmentTooLarge, store_attachment  # noqa: PLC0415

    if req.content_b64 is not None:
        try:
            content = base64.b64decode(req.content_b64, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(status_code=400, detail="content_b64 is not valid base64")
    else:
        content = f"%PDF-1.4 sandbox attachment for {case_id}\n".encode("utf-8")

    try:
        rec = store_attachment(
            tenant_id, req.name, req.mime_type, content, case_id=case_id,
        )
    except AttachmentTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc))

    return {
        "attachment_id": rec.id,
        "case_id": case_id,
        "name": rec.name,
        "mime_type": rec.mime_type,
        "sha256": rec.sha256,
        "bytes": rec.size_bytes,
        "ok": True,
    }


# ---------------------------------------------------------------------------
# ADR-043 P1.1 — seed an EMAIL_ENTRY case + stored attachment + projected
# EvidenceAnchors so the preview safety bar shows *located* highlights.
# Spec: docs/specs/sandbox-attachment-anchor-seed.md. Sandbox-only; a
# stand-in for the ADR-036 email-intelligence-agent producer. The bytes
# CONTAIN `document_text` so the rendered text layer is extractable and the
# composer-derived anchors locate.
# ---------------------------------------------------------------------------


def _mock_pdf_bytes(text: str) -> bytes:
    """Minimal, openable one-page PDF embedding ``text`` as a text layer.

    Mirrors ``asoe-ui/src/lib/mock-data/attachment-bytes.ts::makeMinimalPdf``
    (one text run per line, ASCII, valid xref/%%EOF) so the seeded "real"
    bytes and the UI's mock bytes locate identically. xref offsets are byte
    offsets computed against the encoded body.
    """
    lines = text.split("\n") or [""]
    safe = [
        l.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        for l in (lines if any(lines) else ["Mock attachment"])
    ]
    content = "BT /F1 12 Tf 64 720 Td"
    for i, l in enumerate(safe):
        content += f" ({l}) Tj" if i == 0 else f" 0 -18 Td ({l}) Tj"
    content += " ET"
    content_len = len(content.encode("latin-1", "replace"))
    objects = [
        "<</Type /Catalog /Pages 2 0 R>>",
        "<</Type /Pages /Kids [3 0 R] /Count 1>>",
        "<</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources <</Font <</F1 5 0 R>>>> /Contents 4 0 R>>",
        f"<</Length {content_len}>>\nstream\n{content}\nendstream",
        "<</Type /Font /Subtype /Type1 /BaseFont /Helvetica>>",
    ]
    body = "%PDF-1.4\n"
    offsets: List[int] = []
    for i, obj in enumerate(objects):
        offsets.append(len(body.encode("latin-1", "replace")))
        body += f"{i + 1} 0 obj\n{obj}\nendobj\n"
    xref = len(body.encode("latin-1", "replace"))
    body += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    for off in offsets:
        body += f"{off:010d} 00000 n \n"
    body += (
        f"trailer\n<</Size {len(objects) + 1} /Root 1 0 R>>\n"
        f"startxref\n{xref}\n%%EOF"
    )
    return body.encode("latin-1", "replace")


def _seed_bytes(document_text: str, mime_type: str) -> bytes:
    """Build attachment bytes that contain ``document_text`` for the given
    MIME so the anchor text is locatable. PDF → embedded text layer;
    everything else → the raw UTF-8 text (still downloadable; the preview
    default-denies non-allowlisted types, which is the correct behaviour)."""
    if mime_type == "application/pdf":
        return _mock_pdf_bytes(document_text)
    return document_text.encode("utf-8")


class AnchorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    label: str
    supports_ref: str


class SeedEmailAttachmentAnchorsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_text: str
    attachment_name: str
    attachment_mime: str
    anchors: List[AnchorSpec]
    subject: Optional[str] = None
    from_address: Optional[str] = None


def _create_email_entry_exception(
    tenant_id: str, *, subject: str, from_address: str,  # noqa: ARG001
) -> tuple[str, Optional[str]]:
    """Open an EMAIL_ENTRY case + exception via the normal manual-intake
    graph path. Returns (exception_id, case_id). Reuses the existing
    seed-manual-order-intake building blocks so no new business logic is
    introduced — the case is materialised eagerly for MANUAL_ORDER_INTAKE."""
    from api.routes.exceptions import (  # noqa: PLC0415
        _build_order_event,
        _persist_exception,
        _publish_task_complete,
        _resolve_state,
    )
    from api.schemas import ResolveRequest  # noqa: PLC0415
    from contracts.models import GraphState  # noqa: PLC0415

    payload = _build_manual_intake_event_payload(SeedManualOrderIntakeRequest())
    resolve_req = ResolveRequest.model_validate(payload)
    event = _build_order_event(resolve_req)
    state = GraphState(event=event, tenant_id=tenant_id)
    try:
        final_state = _resolve_state(state, tenant_id)
    except Exception as exc:  # pragma: no cover - bubble up like /resolve
        raise HTTPException(status_code=500, detail=f"graph failure: {exc}")
    trace_id = final_state.shadow.trace_id if final_state.shadow else None
    exception_id = _persist_exception(tenant_id, final_state, trace_id)
    _publish_task_complete(tenant_id, exception_id, trace_id or "", final_state)
    record = exception_store.get(exception_id, tenant_id)
    case_id = record.parent_case_id if record else None
    return exception_id, case_id


@router.post(
    "/_sandbox/seed/email-attachment-anchors",
    dependencies=[Depends(require_role("manager", "admin"))],
)
async def seed_email_attachment_anchors(
    req: SeedEmailAttachmentAnchorsRequest,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001
) -> dict:
    """Seed an EMAIL_ENTRY case with a stored attachment whose bytes contain
    ``document_text`` and projected text-derived EvidenceAnchors, so the
    composer (`adapt_email_source` → `build_evidence_anchors`) emits located
    anchors with no new business logic (ADR-043 P1.1)."""
    _require_sandbox()
    from gateways.attachment_store import (  # noqa: PLC0415
        AttachmentTooLarge,
        store_attachment,
    )

    subject = req.subject or "Purchase order attached"
    from_address = req.from_address or "buyer@sandbox.example"

    exception_id, case_id = _create_email_entry_exception(
        tenant_id, subject=subject, from_address=from_address,
    )
    if case_id is None:  # pragma: no cover - manual intake always materialises
        raise HTTPException(
            status_code=500, detail="seeded exception has no parent case",
        )

    content = _seed_bytes(req.document_text, req.attachment_mime)
    try:
        rec = store_attachment(
            tenant_id, req.attachment_name, req.attachment_mime, content,
            case_id=case_id,
        )
    except AttachmentTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc))

    # Stamp enrichment_context so the composer derives located anchors. Merge
    # (never clobber) the gateway contexts the graph already wrote; only the
    # email_source_context + extracted_entities keys are (re)set here.
    record = exception_store.get(exception_id, tenant_id)
    enrichment = dict(record.enrichment_context or {}) if record else {}
    enrichment["email_source_context"] = {
        "from_address": from_address,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "subject": subject,
        "body_hash": hashlib.sha256(req.document_text.encode("utf-8")).hexdigest(),
        "attachment_manifest": [{
            "name": req.attachment_name,
            "mime_type": req.attachment_mime,
            "bytes": rec.size_bytes,
            "sha256": rec.sha256,
            "attachment_id": rec.id,
        }],
        "body_excerpt": req.document_text[:240],
    }
    enrichment["extracted_entities"] = [
        {
            "key": a.supports_ref.rsplit(".", 1)[-1],
            "value": a.text,
            "kind": "field",
            "source_span": a.text,
        }
        for a in req.anchors
    ]
    exception_store.update(exception_id, tenant_id, enrichment_context=enrichment)

    return {
        "exception_id": exception_id,
        "case_id": case_id,
        "attachment_id": rec.id,
        "ok": True,
    }
