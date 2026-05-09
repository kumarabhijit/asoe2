"""OrderCase read endpoints (ADR-038 Phase H.6).

GET /api/v1/cases          — list cases for the caller's tenant, with
                              optional source / status filters.
GET /api/v1/cases/{id}     — fetch a single case.

The case lifecycle is mutated implicitly by the orchestration graph
(lazy materialisation in ``api/case_resolver.py``) and the (still
dormant) Case Agent loop. This surface is read-only on purpose: any
write path that wants to alter case state must go through the
existing override / cosign / disposition flows, which Phase H.7
migrates to operate on the case lifecycle.

RBAC mirrors the exception-list endpoint: analyst, manager, admin,
viewer, and partner can read cases (partner sees only orders prefixed
by their ``retailer_id``; assigned-account users see only their
allowlist). Tenant isolation is enforced server-side via the
``get_tenant_id`` dependency — cross-tenant reads return an empty
list.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query

from api.deps import (
    AuthenticatedUser,
    get_current_user,
    get_tenant_id,
    require_role,
)
from api.errors import ASOEError
from api.schemas import CaseListResponse
from api.store import case_store, exception_store

logger = logging.getLogger("asoe.api.cases")

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scope_to_user(cases, user: AuthenticatedUser):
    """Apply assigned-accounts / partner-retailer scoping to the case list.

    Cases don't carry ``account_id`` directly, so we derive scope from
    the child ``ExceptionRecord`` rows that share the case's
    ``parent_case_id``. A case is in scope when at least one of its
    children is in scope. Cases with no children yet (just-opened
    Manual Orders before any event lands) fall back to the case's
    ``customer_id`` for assigned-accounts and remain hidden from
    partner-role users (no order_id to prefix-match against).
    """
    if not user.assigned_accounts and not (
        "partner" in user.roles and user.retailer_id
    ):
        return list(cases)

    allowed_accounts = set(user.assigned_accounts or [])
    retailer_prefix = user.retailer_id if "partner" in user.roles else None

    scoped = []
    for case in cases:
        children = exception_store.list_by_case(case.tenant_id, case.case_id)
        if allowed_accounts:
            in_scope = any(
                getattr(r, "account_id", None) in allowed_accounts
                for r in children
            )
            if not in_scope and case.customer_id in allowed_accounts:
                in_scope = True
        elif retailer_prefix:
            in_scope = any(
                getattr(r, "order_id", "").startswith(retailer_prefix)
                for r in children
            )
        else:
            in_scope = True
        if in_scope:
            scoped.append(case)
    return scoped


# ---------------------------------------------------------------------------
# GET /api/v1/cases — list cases
# ---------------------------------------------------------------------------

@router.get(
    "/cases",
    response_model=CaseListResponse,
    dependencies=[Depends(require_role(
        "analyst", "manager", "admin", "viewer", "partner",
    ))],
)
async def list_cases(
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
    source: Optional[str] = Query(
        None,
        description="Filter by case source (manual_order | automated_order)",
    ),
    status: Optional[str] = Query(
        None, description="Filter by case status (CaseStatus literal)",
    ),
    limit: int = Query(200, ge=1, le=500),
) -> CaseListResponse:
    cases = case_store.list_by_tenant(tenant_id)
    if source:
        cases = [c for c in cases if c.source == source]
    if status:
        cases = [c for c in cases if c.status == status]

    cases = _scope_to_user(cases, user)
    cases.sort(key=lambda c: c.opened_at, reverse=True)
    capped = cases[:limit]
    return CaseListResponse(
        items=[c.model_dump(mode="json") for c in capped],
        total=len(cases),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/cases/{id} — case detail
# ---------------------------------------------------------------------------

@router.get(
    "/cases/{case_id}",
    dependencies=[Depends(require_role(
        "analyst", "manager", "admin", "viewer", "partner",
    ))],
)
async def get_case(
    case_id: str,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    case = case_store.get(case_id)
    if case is None or case.tenant_id != tenant_id:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"Case {case_id} not found.",
            status_code=404,
        )
    if not _scope_to_user([case], user):
        raise ASOEError(
            code="NOT_FOUND",
            message=f"Case {case_id} not found.",
            status_code=404,
        )
    return case.model_dump(mode="json")
