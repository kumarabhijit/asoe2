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
from api.store import ExceptionRecord


def _record(
    *,
    enrichment_context=None,
    resolution_data=None,
    original_event=None,
    selected_recipe="DuplicatePORecipe.py",
    intent="DUPLICATE_PO",
) -> ExceptionRecord:
    return ExceptionRecord(
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
