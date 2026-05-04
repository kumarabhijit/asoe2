"""ADR-030 — Discriminated-union ConfigChange domain event.

Surfaces every mutation of the DUPLICATE_PO 5-level score-weight
hierarchy (layers 2-5; layer 1 is on-disk and immutable). The four
variants share a base envelope and differ only in the scope keys their
layer requires:

  * tenant   — global per-tenant (no extra scope)
  * tier     — per (tenant, customer_tier)
  * customer — per (tenant, customer_id)
  * channel  — per (tenant, customer_id, channel)

The discriminated union is keyed on the ``layer`` literal so
``model_validate`` / TypeAdapter dispatches to the correct subclass
without manual branching. This is the canonical payload that flows
through:
  * ``policy_audit_log.previous_value`` and ``new_value`` JSONB columns
  * the ConfigChange WebSocket event (PR-C.2)
  * GET /api/v1/config/tenants/{tenant_id}/audit (PR-C.2)

Same ``extra=\"forbid\"`` discipline as ``contracts/duplicate_po_contract.py``
output models — no silent acceptance of unknown fields.
"""

from __future__ import annotations

from typing import Annotated, Dict, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class _ConfigChangeBase(BaseModel):
    """Fields shared by every ConfigChange variant.

    Subclasses add the layer-specific scope discriminators. The
    ``event_type`` literal stays constant across variants — the
    union discriminator is ``layer``.
    """

    model_config = ConfigDict(extra="forbid")
    event_type: Literal["config_change"] = "config_change"
    tenant_id: str
    changed_by: str
    timestamp: str
    new_weights: Dict[str, float]
    previous_weights: Optional[Dict[str, float]] = None
    change_reason: Optional[str] = None


class TenantLayerConfigChange(_ConfigChangeBase):
    """Edit to layer 2 — global per-tenant override.

    Affects every event tagged with ``tenant_id`` regardless of
    customer / tier / channel.
    """

    layer: Literal["tenant"] = "tenant"


class TierLayerConfigChange(_ConfigChangeBase):
    """Edit to layer 3 — per-customer-tier override.

    Affects every event whose customer is bucketed into the named
    tier. Tiers are an enumerated vocabulary (strategic / standard /
    smb) so consumers can rely on the literal type.
    """

    layer: Literal["tier"] = "tier"
    customer_tier: Literal["strategic", "standard", "smb"]


class CustomerLayerConfigChange(_ConfigChangeBase):
    """Edit to layer 4 — per-customer override.

    customer_id is the tenant-scoped identifier (not globally unique).
    """

    layer: Literal["customer"] = "customer"
    customer_id: str


class ChannelLayerConfigChange(_ConfigChangeBase):
    """Edit to layer 5 — per-(customer, channel) override.

    The narrowest layer. Channel is provider-defined (e.g. EDI, PORTAL).
    """

    layer: Literal["channel"] = "channel"
    customer_id: str
    channel: str


ConfigChangeEvent = Annotated[
    Union[
        TenantLayerConfigChange,
        TierLayerConfigChange,
        CustomerLayerConfigChange,
        ChannelLayerConfigChange,
    ],
    Field(discriminator="layer"),
]


def policy_key_for_event(event: _ConfigChangeBase) -> str:
    """Encode a ConfigChange event into the policy_audit_log policy_key.

    The encoding is stable across releases — auditors can grep the
    audit log by policy_key prefix to find every edit at a given
    layer / scope.

    Schema:
      tenant   → "duplicate_po.weights.tenant"
      tier     → "duplicate_po.weights.tier:{customer_tier}"
      customer → "duplicate_po.weights.customer:{customer_id}"
      channel  → "duplicate_po.weights.channel:{customer_id}:{channel}"
    """
    layer = event.layer
    if layer == "tenant":
        return "duplicate_po.weights.tenant"
    if layer == "tier":
        return f"duplicate_po.weights.tier:{event.customer_tier}"  # type: ignore[attr-defined]
    if layer == "customer":
        return f"duplicate_po.weights.customer:{event.customer_id}"  # type: ignore[attr-defined]
    if layer == "channel":
        return (
            f"duplicate_po.weights.channel:"
            f"{event.customer_id}:{event.channel}"  # type: ignore[attr-defined]
        )
    raise ValueError(f"unknown layer: {layer!r}")
