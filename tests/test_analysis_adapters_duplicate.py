"""T2 adapter tests — DuplicatePO → DuplicateDetectionData +
synthesised OrderComparisonData.

Source bags exercised:
  * `record.enrichment_context["matched_po_details"]` — gateway-fetched
    audit-bearing OrderSnapshot pair (registry: original_order +
    duplicate_order).
  * `record.resolution_data` — recipe composite_score +
    recommended_action + autonomy_level.

Invariants:
  * Happy path: every audit-bearing field populates from the gateway
    payload; secondary OrderComparisonData synthesises from the same
    source.
  * Missing OrderSnapshot pair → adapter returns None →
    AUDIT_CONTEXT_MISSING (no grandfather clause).
  * Empty resolution_data (explain mode / shadow-gated) →
    synthesises recipe output via pure recipe call.
"""

from __future__ import annotations

from api.analysis_adapters import adapt_duplicate, adapt_order_comparison
from api.store import ChildCase


def _record(
    *,
    enrichment_context=None,
    resolution_data=None,
    original_event=None,
    selected_recipe="DuplicatePORecipe.py",
    intent="DUPLICATE_PO",
) -> ChildCase:
    return ChildCase(
        tenant_id="t1",
        order_id="PO-DUP-001",
        event_type="EDI_850_DUPLICATE_PO",
        trace_id="trace-1",
        intent=intent,
        selected_recipe=selected_recipe,
        resolution_data=resolution_data or {},
        original_event=original_event,
        enrichment_context=enrichment_context or {},
    )


def _matched_payload(**overrides):
    """Default audit-complete matched_po_details gateway payload."""
    base = {
        "has_revision_indicator": False,
        "line_items_identical": True,
        "days_between": 1,
        "cancellation_target": "SO-DUP-002",
        "detection_method": "po_number+customer+lines",
        "customer_id": "R-10",
        "matching_fields": ["po_number", "customer_id", "line_items"],
        "differing_fields": [],
        "original_order": {
            "so_number": "SO-DUP-001",
            "po_number": "PO-4000",
            "created_date": "2026-04-20",
            "total_value": 1000.0,
            "line_count": 1,
            "status": "OPEN",
            "lines": [{"sku": "SKU-A", "description": "Widget", "qty": 10.0, "unit_price": 100.0}],
        },
        "duplicate_order": {
            "so_number": "SO-DUP-002",
            "po_number": "PO-4001",
            "created_date": "2026-04-21",
            "total_value": 1000.0,
            "line_count": 1,
            "status": "OPEN",
            "lines": [{"sku": "SKU-A", "description": "Widget", "qty": 10.0, "unit_price": 100.0}],
        },
    }
    base.update(overrides)
    return base


def _high_signal_event() -> dict:
    return {
        "order_id": "PO-DUP-001",
        "retailer_id": "R-10",
        "metadata": {
            "signal_scores": {
                "po_number": 1.0,
                "customer_id": 1.0,
                "line_items": 0.95,
                "amount": 0.90,
                "timestamp": 0.80,
                "ship_to": 0.80,
                "channel": 1.0,
                "delivery_date": 0.80,
            },
        },
    }


class TestAdaptDuplicateHappyPath:
    def test_projects_every_audit_bearing_field(self):
        record = _record(
            enrichment_context={"matched_po_details": _matched_payload()},
            resolution_data={
                "composite_score": 0.95,
                "classification": "AUTO_BLOCK",
                "recommended_action": "BLOCK_AND_NOTIFY",
                "autonomy_level": "L3",
                "status": "BLOCKED",
            },
        )
        result = adapt_duplicate(record)
        assert result is not None
        # Gateway-sourced audit-bearing fields
        assert result.original_order.so_number == "SO-DUP-001"
        assert result.original_order.po_number == "PO-4000"
        assert result.duplicate_order.so_number == "SO-DUP-002"
        assert result.duplicate_order.po_number == "PO-4001"
        assert result.days_between == 1
        assert result.cancellation_target == "SO-DUP-002"
        # Recipe-output audit-bearing fields
        assert result.confidence == 95.0
        # ADR-032 — signal projected from the 0-1 composite_score (not the
        # *100 display value), uncalibrated until the loop ships.
        assert result.confidence_signal is not None
        assert result.confidence_signal.value == 0.95
        assert result.confidence_signal.calibrated is False
        assert result.recommended_action == "BLOCK_AND_NOTIFY"
        assert result.autonomy_applied == "L3 — BLOCK_AND_NOTIFY"
        # Contextual
        assert result.detection_method == "po_number+customer+lines"

    def test_omits_detection_method_when_absent(self):
        payload = _matched_payload()
        payload.pop("detection_method")
        record = _record(
            enrichment_context={"matched_po_details": payload},
            resolution_data={
                "composite_score": 0.95,
                "recommended_action": "BLOCK_AND_NOTIFY",
                "autonomy_level": "L3",
            },
        )
        result = adapt_duplicate(record)
        assert result is not None
        assert result.detection_method is None


class TestAdaptDuplicateMissingGatewayEvidence:
    def test_no_enrichment_context_returns_none(self):
        record = _record(
            resolution_data={"recommended_action": "BLOCK_AND_NOTIFY"},
        )
        assert adapt_duplicate(record) is None

    def test_missing_original_order_returns_none(self):
        payload = _matched_payload()
        payload.pop("original_order")
        record = _record(
            enrichment_context={"matched_po_details": payload},
        )
        assert adapt_duplicate(record) is None

    def test_missing_duplicate_order_returns_none(self):
        payload = _matched_payload()
        payload.pop("duplicate_order")
        record = _record(
            enrichment_context={"matched_po_details": payload},
        )
        assert adapt_duplicate(record) is None

    def test_partial_snapshot_returns_none(self):
        """OrderSnapshot has 6 audit-bearing subfields per the registry.
        Any one missing → adapter returns None → composer routes to
        AUDIT_CONTEXT_MISSING."""
        payload = _matched_payload()
        del payload["original_order"]["status"]
        record = _record(
            enrichment_context={"matched_po_details": payload},
        )
        assert adapt_duplicate(record) is None


class TestAdaptDuplicateSynthesisFallback:
    """Explain mode + shadow-gated YELLOW/RED records have no recipe
    output. The adapter synthesises by calling the pure recipe
    function — same pattern as adapt_price_hold."""

    def test_synthesizes_when_resolution_data_empty(self):
        record = _record(
            enrichment_context={"matched_po_details": _matched_payload()},
            resolution_data={},
            original_event=_high_signal_event(),
        )
        result = adapt_duplicate(record)
        assert result is not None
        # Recipe synthesises AUTO_BLOCK at composite > 0.90.
        assert result.recommended_action != ""
        assert result.autonomy_applied != ""
        assert result.confidence > 0.0

    def test_synthesis_falls_back_to_empty_when_no_signal_scores(self):
        record = _record(
            enrichment_context={"matched_po_details": _matched_payload()},
            resolution_data={},
            original_event={"order_id": "X", "retailer_id": "R-1", "metadata": {}},
        )
        # No signal_scores → recipe defaults all to 0.0 → PASS classification.
        result = adapt_duplicate(record)
        assert result is not None
        assert result.confidence == 0.0


class TestAdaptDuplicateSynthesisHonoursTenantWeights:
    """Regression (V&V audit 2026-06-06): the synthetic projection used on
    the explain / shadow-gated path (empty resolution_data) MUST resolve the
    same tenant-specific signal weights (ADR-029) that the live execution
    path plumbs into detect_duplicate_po(weights=...) at
    orchestration/nodes.py::validate_types. Otherwise the confidence /
    recommended_action shown to the operator silently diverges from the
    tenant's configured policy for any tenant with non-default weights.

    Signal scores below fire only po_number (1.0) and line_items (1.0); every
    other signal is 0.0. With the platform-default _WEIGHTS the composite is
    0.30 + 0.20 = 0.50 (confidence 50.0). The tenant weight map below shifts
    weight off those two signals, so the faithful composite is
    0.10 + 0.10 = 0.20 (confidence 20.0). A build that ignores the resolved
    weights would report 50.0.
    """

    _ONLY_PO_AND_LINES_EVENT = {
        "order_id": "PO-DUP-001",
        "retailer_id": "R-10",
        "metadata": {
            "signal_scores": {
                "po_number": 1.0,
                "customer_id": 0.0,
                "line_items": 1.0,
                "amount": 0.0,
                "timestamp": 0.0,
                "ship_to": 0.0,
                "channel": 0.0,
                "delivery_date": 0.0,
            },
        },
    }

    # Valid ADR-029 map (same keys, sums to 1.0) that de-emphasises the two
    # signals the event fires.
    _TENANT_WEIGHTS = {
        "po_number": 0.10,
        "customer_id": 0.30,
        "line_items": 0.10,
        "amount": 0.10,
        "timestamp": 0.10,
        "ship_to": 0.10,
        "channel": 0.10,
        "delivery_date": 0.10,
    }

    def test_synthesis_uses_resolved_tenant_weights(self):
        record = _record(
            enrichment_context={
                "matched_po_details": _matched_payload(),
                "tenant_config": {"weights": self._TENANT_WEIGHTS},
            },
            resolution_data={},
            original_event=self._ONLY_PO_AND_LINES_EVENT,
        )
        result = adapt_duplicate(record)
        assert result is not None
        # 0.10*1.0 (po_number) + 0.10*1.0 (line_items) = 0.20 → *100 display.
        assert result.confidence == 20.0
        assert result.confidence_signal is not None
        assert result.confidence_signal.value == 0.20

    def test_synthesis_falls_back_to_platform_weights_when_absent(self):
        """No tenant_config → weights=None → recipe's module-default
        _WEIGHTS, matching orchestration's `tenant_config.get("weights")`
        (which yields None) behaviour. Composite 0.30 + 0.20 = 0.50."""
        record = _record(
            enrichment_context={"matched_po_details": _matched_payload()},
            resolution_data={},
            original_event=self._ONLY_PO_AND_LINES_EVENT,
        )
        result = adapt_duplicate(record)
        assert result is not None
        assert result.confidence == 50.0


class TestAdaptOrderComparison:
    def test_synthesises_two_orders_from_matched_payload(self):
        record = _record(
            enrichment_context={"matched_po_details": _matched_payload()},
        )
        result = adapt_order_comparison(record)
        assert result is not None
        assert len(result.orders) == 2
        assert result.orders[0].so_number == "SO-DUP-001"
        assert result.orders[1].so_number == "SO-DUP-002"
        assert result.orders[0].customer == "R-10"
        assert len(result.orders[0].lines) == 1
        assert result.orders[0].lines[0].sku == "SKU-A"
        assert result.matching_fields == ["po_number", "customer_id", "line_items"]
        assert result.differing_fields == []

    def test_returns_none_when_either_snapshot_missing(self):
        payload = _matched_payload()
        payload.pop("duplicate_order")
        record = _record(enrichment_context={"matched_po_details": payload})
        assert adapt_order_comparison(record) is None

    def test_falls_back_to_event_retailer_id_for_customer(self):
        payload = _matched_payload()
        payload.pop("customer_id")
        record = _record(
            enrichment_context={"matched_po_details": payload},
            original_event={"order_id": "PO-X", "retailer_id": "R-FALLBACK"},
        )
        result = adapt_order_comparison(record)
        assert result is not None
        assert result.orders[0].customer == "R-FALLBACK"


class TestRegistryWiring:
    def test_duplicate_recipe_registered_as_primary(self):
        from api.analysis_adapters import ANALYSIS_ADAPTERS
        assert "DuplicatePORecipe.py" in ANALYSIS_ADAPTERS
        field, _adapter = ANALYSIS_ADAPTERS["DuplicatePORecipe.py"]
        assert field == "duplicate_detection"

    def test_order_comparison_registered_as_secondary(self):
        from api.analysis_adapters import SECONDARY_ANALYSIS_ADAPTERS
        secondaries = SECONDARY_ANALYSIS_ADAPTERS["DuplicatePORecipe.py"]
        fields = [f for f, _ in secondaries]
        assert "order_comparison" in fields

    def test_duplicate_po_intent_routes_to_recipe(self):
        from api.analysis_adapters import INTENT_TO_RECIPE_NAME
        assert INTENT_TO_RECIPE_NAME["DUPLICATE_PO"] == "DuplicatePORecipe.py"
