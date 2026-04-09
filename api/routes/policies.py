"""PUT /api/v1/policies/{tenant_id} — Policy overrides (architecture_v3.md Section 8.2).

Admin-only endpoint for updating tenant-specific policy overrides.
V1 uses an in-memory store. PostgreSQL ``policy_overrides`` table
replaces this when the database layer is built.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict
from uuid import uuid4

from fastapi import APIRouter, Depends

from api.deps import AuthenticatedUser, get_current_user, require_role
from api.schemas import PolicyOverrideResponse, PolicyUpdateRequest

router = APIRouter()

# In-memory policy override store — replaced by PostgreSQL policy_overrides table.
_policy_store: Dict[str, Dict[str, Any]] = {}
_policy_lock = threading.Lock()


@router.put(
    "/policies/{tenant_id}",
    response_model=PolicyOverrideResponse,
    dependencies=[Depends(require_role("admin"))],
)
async def update_policy(
    tenant_id: str,
    req: PolicyUpdateRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PolicyOverrideResponse:
    now = datetime.now(timezone.utc).isoformat()
    record_id = str(uuid4())

    with _policy_lock:
        if tenant_id not in _policy_store:
            _policy_store[tenant_id] = {}
        _policy_store[tenant_id][req.policy_key] = {
            "id": record_id,
            "value": req.value,
            "effective_from": now,
            "created_by": user.sub,
            "change_reason": req.change_reason,
        }

    return PolicyOverrideResponse(
        id=record_id,
        tenant_id=tenant_id,
        policy_key=req.policy_key,
        value=req.value,
        effective_from=now,
        created_by=user.sub,
    )
