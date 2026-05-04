"""Unit tests for contracts/config_events.py — discriminated-union dispatch."""

import pytest
from pydantic import TypeAdapter, ValidationError

from contracts.config_events import (
    ChannelLayerConfigChange,
    ConfigChangeEvent,
    CustomerLayerConfigChange,
    TenantLayerConfigChange,
    TierLayerConfigChange,
    policy_key_for_event,
)


_ADAPTER = TypeAdapter(ConfigChangeEvent)


def _base_payload() -> dict:
    return {
        "event_type": "config_change",
        "tenant_id": "tenant-1",
        "changed_by": "alice@example.com",
        "timestamp": "2026-05-04T00:00:00Z",
        "new_weights": {"po_number": 0.4, "vendor_id": 0.3},
        "previous_weights": {"po_number": 0.3, "vendor_id": 0.4},
        "change_reason": "Q2 ramp",
    }


def test_tenant_layer_round_trip():
    payload = {**_base_payload(), "layer": "tenant"}
    event = _ADAPTER.validate_python(payload)
    assert isinstance(event, TenantLayerConfigChange)
    assert event.layer == "tenant"


def test_tier_layer_requires_customer_tier():
    payload = {**_base_payload(), "layer": "tier"}
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python(payload)


def test_tier_layer_round_trip():
    payload = {**_base_payload(), "layer": "tier", "customer_tier": "strategic"}
    event = _ADAPTER.validate_python(payload)
    assert isinstance(event, TierLayerConfigChange)
    assert event.customer_tier == "strategic"


def test_tier_layer_rejects_unknown_tier():
    payload = {**_base_payload(), "layer": "tier", "customer_tier": "premium"}
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python(payload)


def test_customer_layer_round_trip():
    payload = {**_base_payload(), "layer": "customer", "customer_id": "CUST-9"}
    event = _ADAPTER.validate_python(payload)
    assert isinstance(event, CustomerLayerConfigChange)
    assert event.customer_id == "CUST-9"


def test_channel_layer_round_trip():
    payload = {
        **_base_payload(),
        "layer": "channel",
        "customer_id": "CUST-9",
        "channel": "EDI",
    }
    event = _ADAPTER.validate_python(payload)
    assert isinstance(event, ChannelLayerConfigChange)
    assert event.channel == "EDI"


def test_extra_forbid_rejects_unknown_field():
    payload = {**_base_payload(), "layer": "tenant", "rogue_field": True}
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python(payload)


def test_unknown_layer_rejected():
    payload = {**_base_payload(), "layer": "rogue"}
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python(payload)


def test_policy_key_encoding_tenant():
    event = TenantLayerConfigChange(
        tenant_id="t1", changed_by="alice", timestamp="...",
        new_weights={"po_number": 0.4},
    )
    assert policy_key_for_event(event) == "duplicate_po.weights.tenant"


def test_policy_key_encoding_tier():
    event = TierLayerConfigChange(
        tenant_id="t1", changed_by="alice", timestamp="...",
        new_weights={"po_number": 0.4}, customer_tier="strategic",
    )
    assert policy_key_for_event(event) == "duplicate_po.weights.tier:strategic"


def test_policy_key_encoding_customer():
    event = CustomerLayerConfigChange(
        tenant_id="t1", changed_by="alice", timestamp="...",
        new_weights={"po_number": 0.4}, customer_id="CUST-9",
    )
    assert policy_key_for_event(event) == "duplicate_po.weights.customer:CUST-9"


def test_policy_key_encoding_channel():
    event = ChannelLayerConfigChange(
        tenant_id="t1", changed_by="alice", timestamp="...",
        new_weights={"po_number": 0.4}, customer_id="CUST-9", channel="EDI",
    )
    assert policy_key_for_event(event) == "duplicate_po.weights.channel:CUST-9:EDI"
