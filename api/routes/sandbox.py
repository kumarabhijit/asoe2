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
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from api.deps import AuthenticatedUser, get_current_user, get_tenant_id, require_role
from api.store import exception_store

router = APIRouter()


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
