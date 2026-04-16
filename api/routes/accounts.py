"""GET /api/v1/accounts — Retail customer accounts within a tenant.

Accounts are the first-class "customer" entity. Each exception is
associated with an account_id. Users may be scoped to specific accounts
via assigned_accounts on their JWT.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import get_current_user, get_tenant_id, require_role, AuthenticatedUser
from api.schemas import AccountListResponse, AccountResponse
from api.users import list_accounts

router = APIRouter()


@router.get(
    "/accounts",
    response_model=AccountListResponse,
    dependencies=[Depends(require_role("analyst", "manager", "admin", "viewer"))],
)
async def get_accounts(
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
) -> AccountListResponse:
    """Return all accounts for the tenant. Scoped users see only their assigned accounts."""
    accounts = list_accounts(tenant_id)

    # If user has assigned_accounts, filter to only those
    if user.assigned_accounts:
        allowed = set(user.assigned_accounts)
        accounts = [a for a in accounts if a.id in allowed]

    return AccountListResponse(
        data=[
            AccountResponse(
                id=a.id,
                name=a.name,
                tenant_id=a.tenant_id,
                bp_number=a.bp_number,
                tier=a.tier,
                region=a.region,
            )
            for a in accounts
        ]
    )
