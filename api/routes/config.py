"""ADR-030 — Tenant-config admin endpoints (action item A9).

Five endpoints under ``/api/v1/config/tenants/{tenant_id}``:

  GET  /resolve              — resolve full hierarchy for a scope
                                (delegates to TenantConfigGateway).
  GET  /layers/{layer}       — list all rows at a layer.
  PUT  /layers/{layer}       — admin-only upsert; emits ConfigChange
                                via PolicyRepository.create_audit_event.
  DELETE /layers/{layer}     — admin-only delete; emits ConfigChange
                                with ``new_weights={}``.
  GET  /audit                — list ConfigChange entries from the
                                hash-chained policy_audit_log
                                (filtered by policy_key prefix).

Audit-chain wiring is the route handler's responsibility — every
upsert and delete writes a typed ``ConfigChangeEvent`` payload through
``PolicyRepository.create_audit_event``, which hashes it into the
existing append-only ``policy_audit_log`` (V003 trigger guards
UPDATE/DELETE at the DB level). The single SOX surface covers both
policy threshold tunings and config-weight edits.

Dependency injection:
  * ``get_tenant_config_repo`` / ``get_policy_repo`` wrap the shared
    process adapter (``db.shared.get_shared_adapter``) so route
    handlers see the same DB across requests.
  * Tests override via ``app.dependency_overrides[...]`` to inject a
    freshly migrated in-memory adapter per case.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from api.deps import (
    AuthenticatedUser,
    get_current_user,
    require_role,
)
from contracts.config_events import (
    ConfigChangeEvent,
    policy_key_for_event,
)
from contracts.models import GatewayRequest
from db.repository import PolicyRepository, TenantConfigRepository
from db.shared import get_shared_adapter
from gateways.registry import get_gateway

router = APIRouter()

_LAYERS = ("tenant", "tier", "customer", "channel")
_CONFIG_AUDIT_PREFIX = "duplicate_po.weights."


# ---------------------------------------------------------------------------
# Dependency providers (overridable in tests)
# ---------------------------------------------------------------------------


def get_tenant_config_repo() -> TenantConfigRepository:
    """Provider for ``TenantConfigRepository`` bound to the shared adapter."""
    return TenantConfigRepository(adapter=get_shared_adapter())


def get_policy_repo() -> PolicyRepository:
    """Provider for ``PolicyRepository`` bound to the shared adapter."""
    return PolicyRepository(adapter=get_shared_adapter())


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class TenantConfigUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Dict[str, Any] = Field(default_factory=dict)
    weights: Dict[str, float]
    change_reason: Optional[str] = None


class TenantConfigDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Dict[str, Any] = Field(default_factory=dict)
    change_reason: Optional[str] = None


class TenantConfigRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    tenant_id: str
    layer: str
    scope_hash: str
    scope: Dict[str, Any]
    weights: Dict[str, float]
    created_by: str
    created_at: str
    updated_at: str


class ConfigLayerListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    layer: str
    rows: List[TenantConfigRow]


class ConfigResolveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    weights: Dict[str, float]
    contribution_trace: List[Dict[str, Any]]
    validation_status: str
    violation_reason: Optional[str] = None
    scope: Dict[str, Any]


class ConfigAuditEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    tenant_id: str
    policy_key: str
    previous_value: Optional[Any] = None
    new_value: Any
    changed_by: str
    change_reason: Optional[str] = None
    created_at: str
    prev_hash: str
    event_hash: str


class ConfigAuditResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    entries: List[ConfigAuditEntry]


class ConfigDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deleted: bool
    tenant_id: str
    layer: str
    scope: Dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_layer_name(layer: str) -> None:
    if layer not in _LAYERS:
        raise HTTPException(
            status_code=404,
            detail=f"unknown layer {layer!r}; expected one of {_LAYERS}",
        )


def _validate_scope_for_layer(layer: str, scope: Dict[str, Any]) -> None:
    """Ensure the scope keys match the layer's required shape.

    Mirrors the discriminated-union variants in
    ``contracts.config_events`` so the API surface and the audit-event
    payload stay in lockstep.
    """
    if layer == "tenant":
        if scope:
            raise HTTPException(
                400, "tenant layer takes empty scope (no scope keys)"
            )
    elif layer == "tier":
        if "customer_tier" not in scope:
            raise HTTPException(
                400, "tier layer requires customer_tier in scope"
            )
        if scope["customer_tier"] not in ("strategic", "standard", "smb"):
            raise HTTPException(
                400,
                "customer_tier must be one of strategic|standard|smb",
            )
    elif layer == "customer":
        if "customer_id" not in scope:
            raise HTTPException(
                400, "customer layer requires customer_id in scope"
            )
    elif layer == "channel":
        if "customer_id" not in scope or "channel" not in scope:
            raise HTTPException(
                400,
                "channel layer requires customer_id and channel in scope",
            )


def _build_config_change_event(
    *,
    layer: str,
    tenant_id: str,
    scope: Dict[str, Any],
    new_weights: Dict[str, float],
    previous_weights: Optional[Dict[str, float]],
    changed_by: str,
    change_reason: Optional[str],
):
    """Build a typed ConfigChangeEvent via the discriminated union.

    TypeAdapter dispatches to the correct subclass based on the
    ``layer`` field; the union enforces that scope keys required for
    each layer are present (e.g. ``customer_tier`` for tier).
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    payload = {
        "event_type": "config_change",
        "layer": layer,
        "tenant_id": tenant_id,
        "changed_by": changed_by,
        "timestamp": timestamp,
        "new_weights": new_weights,
        "previous_weights": previous_weights,
        "change_reason": change_reason,
        **{k: v for k, v in scope.items()},
    }
    return TypeAdapter(ConfigChangeEvent).validate_python(payload)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/config/tenants/{tenant_id}/resolve",
    response_model=ConfigResolveResponse,
    dependencies=[Depends(get_current_user)],
)
async def resolve_config(
    tenant_id: str,
    customer_tier: Optional[Literal["strategic", "standard", "smb"]] = Query(None),
    customer_id: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    behavior_tag: Optional[str] = Query(None),
) -> ConfigResolveResponse:
    """Resolve the full 5-layer hierarchy for the requested scope.

    Delegates to the registered ``tenant_config`` gateway so the
    resolver path is identical to what the orchestration node sees
    on every event — the API surface and the runtime path can never
    diverge.
    """
    gateway = get_gateway("tenant_config")
    response = gateway.execute(GatewayRequest(
        gateway_name="tenant_config",
        operation="resolve_for_event",
        params={
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "customer_tier": customer_tier,
            "channel": channel,
            "behavior_tag": behavior_tag,
        },
        trace_id=f"resolve-{tenant_id}",
    ))
    data = response.data
    return ConfigResolveResponse(
        tenant_id=tenant_id,
        weights=data["weights"],
        contribution_trace=data["contribution_trace"],
        validation_status=data["validation_status"],
        violation_reason=data.get("violation_reason"),
        scope=data.get("scope", {}),
    )


@router.get(
    "/config/tenants/{tenant_id}/layers/{layer}",
    response_model=ConfigLayerListResponse,
    dependencies=[Depends(require_role("admin", "manager"))],
)
async def list_layer(
    tenant_id: str,
    layer: str,
    config_repo: TenantConfigRepository = Depends(get_tenant_config_repo),
) -> ConfigLayerListResponse:
    _validate_layer_name(layer)
    rows = config_repo.list_by_layer(tenant_id, layer)
    return ConfigLayerListResponse(
        tenant_id=tenant_id,
        layer=layer,
        rows=[TenantConfigRow.model_validate(r) for r in rows],
    )


@router.put(
    "/config/tenants/{tenant_id}/layers/{layer}",
    response_model=TenantConfigRow,
    dependencies=[Depends(require_role("admin"))],
)
async def upsert_layer(
    tenant_id: str,
    layer: str,
    req: TenantConfigUpsertRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    config_repo: TenantConfigRepository = Depends(get_tenant_config_repo),
    policy_repo: PolicyRepository = Depends(get_policy_repo),
) -> TenantConfigRow:
    _validate_layer_name(layer)
    _validate_scope_for_layer(layer, req.scope)

    existing = config_repo.get(tenant_id, layer, req.scope)
    previous_weights = existing["weights"] if existing else None

    # Build + validate the typed event before any DB write so an
    # invalid scope/payload short-circuits with no persisted state.
    event = _build_config_change_event(
        layer=layer, tenant_id=tenant_id, scope=req.scope,
        new_weights=req.weights, previous_weights=previous_weights,
        changed_by=user.sub, change_reason=req.change_reason,
    )

    row = config_repo.upsert(
        tenant_id=tenant_id, layer=layer, scope=req.scope,
        weights=req.weights, created_by=user.sub,
    )

    policy_repo.create_audit_event(
        tenant_id=tenant_id,
        policy_key=policy_key_for_event(event),
        previous_value=event.previous_weights,
        new_value=event.new_weights,
        changed_by=user.sub,
        change_reason=req.change_reason,
    )

    return TenantConfigRow.model_validate(row)


@router.delete(
    "/config/tenants/{tenant_id}/layers/{layer}",
    response_model=ConfigDeleteResponse,
    dependencies=[Depends(require_role("admin"))],
)
async def delete_layer(
    tenant_id: str,
    layer: str,
    req: TenantConfigDeleteRequest = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    config_repo: TenantConfigRepository = Depends(get_tenant_config_repo),
    policy_repo: PolicyRepository = Depends(get_policy_repo),
) -> ConfigDeleteResponse:
    _validate_layer_name(layer)
    _validate_scope_for_layer(layer, req.scope)

    existing = config_repo.get(tenant_id, layer, req.scope)
    if not existing:
        raise HTTPException(
            404,
            "config row not found for given (tenant, layer, scope)",
        )

    # Delete is audited as a config_change with ``new_weights={}`` —
    # the empty dict is the canonical \"removed\" marker for downstream
    # consumers (audit dashboards, alerts).
    event = _build_config_change_event(
        layer=layer, tenant_id=tenant_id, scope=req.scope,
        new_weights={}, previous_weights=existing["weights"],
        changed_by=user.sub, change_reason=req.change_reason,
    )

    config_repo.delete(tenant_id, layer, req.scope)

    policy_repo.create_audit_event(
        tenant_id=tenant_id,
        policy_key=policy_key_for_event(event),
        previous_value=event.previous_weights,
        new_value=event.new_weights,
        changed_by=user.sub,
        change_reason=req.change_reason,
    )

    return ConfigDeleteResponse(
        deleted=True, tenant_id=tenant_id, layer=layer, scope=req.scope,
    )


@router.get(
    "/config/tenants/{tenant_id}/audit",
    response_model=ConfigAuditResponse,
    dependencies=[Depends(require_role("admin", "manager"))],
)
async def list_audit(
    tenant_id: str,
    limit: int = Query(50, ge=1, le=500),
    policy_repo: PolicyRepository = Depends(get_policy_repo),
) -> ConfigAuditResponse:
    """List ConfigChange audit entries for the tenant, newest first.

    Filtered by policy_key prefix ``duplicate_po.weights.`` so unrelated
    audit rows (policy threshold tunings, exception lifecycle events)
    don't leak into the config-admin view.
    """
    rows = policy_repo.list_audit_log(
        tenant_id, limit=limit, policy_key_prefix=_CONFIG_AUDIT_PREFIX,
    )
    return ConfigAuditResponse(
        tenant_id=tenant_id,
        entries=[ConfigAuditEntry.model_validate(r) for r in rows],
    )
