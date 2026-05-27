"""Tests for api.analysis_composer — registry-enforced projection.

Invariants exercised:
  * compose() returns an empty ComposedAnalysis for records without
    an adapter (preserves the existing "no enrichment, no harm"
    baseline).
  * A complete PriceHold record projects without missing audit
    fields (coverage complete).
  * Synthesised PriceHold path (YELLOW shadow, no recipe output)
    also passes coverage — the adapter's synthetic fallback
    populates every registry-declared audit-bearing field.
  * A record whose adapter returns None (bad event) yields every
    enforced field as missing.
  * Conditional fields are only enforced when their predicate
    holds (depends_on against resolved_action).
  * Grandfathered fields in PriceAnalysis do NOT fail coverage
    today (before 2026-06-21 deadline); they DO fail after.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from api.analysis_composer import ComposedAnalysis, compose
from api.store import ChildCase


def _make_record(**overrides) -> ChildCase:
    defaults = dict(
        tenant_id="t-test",
        order_id="ORD-001",
        event_type="GENERIC",
        trace_id="trace-1",
    )
    defaults.update(overrides)
    return ChildCase(**defaults)


def test_compose_returns_empty_when_no_adapter_registered():
    record = _make_record(selected_recipe=None, intent=None)
    result = compose(record)
    assert isinstance(result, ComposedAnalysis)
    assert result.class_name is None
    assert result.projection is None
    assert result.missing_audit_fields == []
    assert result.is_complete is True
    assert result.should_route_to_audit_context_missing is False


def test_compose_complete_for_price_hold_green_path():
    """GREEN shadow (1% variance) → recipe ran → every audit-bearing
    PHR field is populated by the adapter."""
    record = _make_record(
        intent="PRICE_HOLD_RELEASE",
        selected_recipe="PriceHoldReleaseRecipe.py",
        resolution_data={
            "status": "RELEASED",
            "action": "AUTO_RELEASE",
            "variance_pct": 0.01,
            "reason": "Variance within tolerance.",
            "order_id": "ORD-001",
        },
        original_event={
            "order_id": "ORD-001",
            "line_item": 1,
            "po_price": 101.0,
            "sap_base_price": 100.0,
            "metadata": {"price_hold_status": "HELD"},
        },
    )
    result = compose(record)
    assert result.class_name == "PriceHoldAnalysisData"
    assert result.projection is not None
    assert result.missing_audit_fields == []
    assert result.is_complete is True


def test_compose_complete_for_price_hold_yellow_synthetic_path():
    """YELLOW shadow (5% variance) → recipe didn't run → adapter's
    synthetic fallback populates the projection. Registry coverage
    must still be complete."""
    record = _make_record(
        intent="PRICE_HOLD_RELEASE",
        selected_recipe=None,  # shadow gated before select_recipe
        resolution_data={},
        original_event={
            "order_id": "ORD-001",
            "line_item": 1,
            "po_price": 105.0,
            "sap_base_price": 100.0,
            "metadata": {"price_hold_status": "HELD"},
        },
    )
    result = compose(record)
    assert result.class_name == "PriceHoldAnalysisData"
    assert result.projection is not None
    # Synthetic path hits ESCALATE — hold_status="HELD", action="ESCALATE"
    assert result.projection.action == "ESCALATE"
    assert result.missing_audit_fields == []


def test_compose_reports_all_enforced_missing_when_projection_is_none():
    """Adapter can't produce a projection (sap_base_price <= 0)
    → every enforced field is reported missing so the caller can
    route to AUDIT_CONTEXT_MISSING."""
    record = _make_record(
        intent="PRICE_HOLD_RELEASE",
        selected_recipe="PriceHoldReleaseRecipe.py",
        resolution_data={"status": "FAILED", "action": None},
        original_event={
            "order_id": "ORD-001",
            "line_item": 1,
            "po_price": 101.0,
            "sap_base_price": 0.0,  # forces adapter to None
            "metadata": {"price_hold_status": "HELD"},
        },
    )
    result = compose(record)
    assert result.class_name == "PriceHoldAnalysisData"
    assert result.projection is None
    assert result.should_route_to_audit_context_missing is True
    # All seven PHR audit-bearing fields should be flagged.
    assert set(result.missing_audit_fields) >= {
        "hold_status", "po_price", "sap_base_price",
        "variance_pct", "tolerance_pct", "hard_block_pct", "action",
    }


def test_conditional_field_not_enforced_when_predicate_fails():
    """BackOrder alternate_warehouses is conditional on
    resolved_action == ALT_DC. With a different resolved_action the
    field must NOT be in missing_audit_fields — the adapter doesn't
    need to populate it.

    The BackOrder adapter isn't shipped yet, so this test asserts
    the classifier behavior in isolation: when the predicate fails,
    the field is treated as contextual.
    """
    from api.analysis_composer import _required_audit_fields

    record = _make_record(resolved_action="SUBSTITUTE")
    enforced, _grandfathered = _required_audit_fields(
        record, "BackOrderAnalysisData",
    )
    # alternate_warehouses is conditional on ALT_DC; SUBSTITUTE fails
    # the predicate so it should not be enforced.
    assert "alternate_warehouses" not in enforced
    # Unconditional audit-bearing fields should still be enforced.
    assert "ordered_qty" in enforced
    assert "gap_pct" in enforced


def test_conditional_field_enforced_when_predicate_holds():
    from api.analysis_composer import _required_audit_fields

    record = _make_record(resolved_action="ALT_DC")
    enforced, _ = _required_audit_fields(record, "BackOrderAnalysisData")
    assert "alternate_warehouses" in enforced


def test_price_analysis_audit_bearing_fields_enforced_post_retirement():
    """T4 retired price_analysis_gateway_gap. Per the Verdict full-close
    engagement, doc_type / doc_number / rule_id /
    root_cause_category remain audit-bearing AND are enforced today
    — the SAP doc / contract / promotion gateways are wired."""
    from api.analysis_composer import _required_audit_fields

    record = _make_record(resolved_action=None)
    enforced, grandfathered = _required_audit_fields(
        record, "PriceAnalysisData",
    )
    assert "doc_type" in enforced
    assert "doc_number" in enforced
    assert "rule_id" in enforced
    assert "root_cause_category" in enforced
    # No grandfather clause for this section anymore.
    assert grandfathered == []


def test_price_analysis_contract_and_promotion_refs_are_contextual():
    """contract_ref / promotion_ref were reclassified contextual at T4
    retirement — not every line is contract- or promotion-governed,
    so absence is structurally legitimate."""
    from api.analysis_composer import _required_audit_fields

    record = _make_record(resolved_action=None)
    enforced, _ = _required_audit_fields(record, "PriceAnalysisData")
    assert "contract_ref" not in enforced
    assert "promotion_ref" not in enforced


def test_always_audit_bearing_convention_applies():
    """The conventions block lists `action` / `recommended_action` /
    `classification` as always audit-bearing. Even if a future
    registry row classified them `contextual`, the composer must
    upgrade to audit-bearing."""
    from api.analysis_composer import _classify_field

    cls = _classify_field(
        "SomeFutureAnalysisData", "action",
        {"tier": "contextual"}, waived=set(),
    )
    assert cls.tier == "audit-bearing"
