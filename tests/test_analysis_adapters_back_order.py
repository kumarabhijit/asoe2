"""T3 adapter tests — BackOrderResolutionRecipe → BackOrderAnalysisData.

Source bags exercised:
  * `event.metadata` — recipe-input fields (ordered_qty, available_qty,
    unit_price, uom).
  * `record.enrichment_context["inventory_snapshot"]` — gateway
    snapshot (primary_dc + atp_date + alternate_warehouses +
    substitutes + production + inbound_po).
  * `record.resolution_data` — recipe outputs (gap, at_risk,
    resolution_options); synthesised when empty.

Invariants:
  * Happy path: every audit-bearing field projects from
    enrichment_context + recipe outputs.
  * Missing primary_dc → adapter returns None → AUDIT_CONTEXT_MISSING.
  * Missing atp_date → adapter returns None → AUDIT_CONTEXT_MISSING.
  * Synthesis fallback: empty resolution_data triggers a pure recipe
    call so explain mode + shadow-gated paths still get audit-complete
    projections (shadow paths still route to AUDIT_CONTEXT_MISSING
    when gateway data is absent — see test_e2e_om_adjacent_intents).
"""

from __future__ import annotations

from api.analysis_adapters import adapt_back_order
from api.store import ChildCase


def _record(
    *,
    enrichment_context=None,
    resolution_data=None,
    original_event=None,
    selected_recipe="BackOrderResolutionRecipe.py",
    intent="BACK_ORDER",
) -> ChildCase:
    return ChildCase(
        tenant_id="t1",
        order_id="PO-BO-100-50",
        event_type="BACK_ORDER_OOS",
        trace_id="trace-1",
        intent=intent,
        selected_recipe=selected_recipe,
        resolution_data=resolution_data or {},
        original_event=original_event,
        enrichment_context=enrichment_context or {},
    )


def _event(ordered=100.0, available=50.0, unit_price=10.0, uom="CS") -> dict:
    return {
        "order_id": "PO-BO-100-50",
        "sku": "SKU-BO-1",
        "metadata": {
            "ordered_qty": ordered,
            "available_qty": available,
            "unit_price": unit_price,
            "uom": uom,
        },
    }


def _inventory_snapshot(**overrides) -> dict:
    base = {
        "primary_dc": {
            "plant": "DC-EAST", "name": "East DC",
            "region": "US-EAST", "qty": 50.0,
        },
        "atp_date": "2026-04-30",
        "alternate_warehouses": [
            {
                "plant": "DC-WEST", "name": "West DC",
                "region": "US-WEST", "qty": 200.0,
                "eta_days": 4, "freight_delta_per_unit": 0.50,
                "freight_delta_total": 25.0,
            },
        ],
        "substitutes": [
            {
                "sku": "SKU-BO-1-ALT", "description": "Equivalent SKU",
                "available_qty": 150.0, "price_delta_pct": 0.02,
                "acceptance_rate": 0.85, "source": "catalog", "priority": 1,
            },
        ],
        "production": {"qty": 100.0, "date": "2026-05-05"},
        "inbound_po": {"qty": 75.0, "eta": "2026-05-02", "po_num": "PO-INB-1"},
    }
    base.update(overrides)
    return base


class TestAdaptBackOrderHappyPath:
    def test_projects_every_audit_bearing_field(self):
        record = _record(
            original_event=_event(),
            enrichment_context={"inventory_snapshot": _inventory_snapshot()},
            resolution_data={
                "status": "REVIEW_REQUIRED",
                "classification": "MINOR_GAP",
                "gap_qty": 50.0, "gap_pct": 0.5, "at_risk": 500.0,
                "recommended_action": "ALT_DC",
                "resolution_options": [
                    {
                        "id": "opt-1", "type": "ALT_DC",
                        "title": "Ship from West DC", "description": "...",
                        "composite_score": 0.85,
                        "scores": {"service": 0.9, "revenue": 0.8,
                                   "logistics": 0.7, "preference": 0.9},
                        "sap_steps": ["VL01N", "VL02N"],
                    },
                ],
            },
        )
        result = adapt_back_order(record)
        assert result is not None
        # Recipe-input audit-bearing
        assert result.ordered_qty == 100.0
        assert result.available_qty == 50.0
        assert result.unit_price == 10.0
        assert result.uom == "CS"
        # Recipe-output audit-bearing
        assert result.gap_qty == 50.0
        assert result.gap_pct == 0.5
        assert result.at_risk == 500.0
        # Gateway audit-bearing
        assert result.atp_date == "2026-04-30"
        assert result.primary_dc.plant == "DC-EAST"
        assert result.primary_dc.qty == 50.0
        # Gateway conditional
        assert len(result.alternate_warehouses) == 1
        assert result.alternate_warehouses[0].plant == "DC-WEST"
        assert len(result.substitutes) == 1
        assert result.substitutes[0].sku == "SKU-BO-1-ALT"
        assert result.production is not None and result.production.qty == 100.0
        assert result.inbound_po is not None and result.inbound_po.po_num == "PO-INB-1"
        # Recipe output: resolution_options
        assert len(result.resolution_options) == 1
        assert result.resolution_options[0].id == "opt-1"
        assert result.resolution_options[0].scores.service == 0.9
        assert result.resolution_options[0].sap_steps == ["VL01N", "VL02N"]

    def test_omits_conditional_blocks_when_empty(self):
        snapshot = _inventory_snapshot(
            alternate_warehouses=[], substitutes=[],
            production=None, inbound_po=None,
        )
        record = _record(
            original_event=_event(),
            enrichment_context={"inventory_snapshot": snapshot},
            resolution_data={"recommended_action": "SPLIT_SHIPMENT",
                             "gap_qty": 50.0, "gap_pct": 0.5, "at_risk": 500.0,
                             "resolution_options": []},
        )
        result = adapt_back_order(record)
        assert result is not None
        assert result.alternate_warehouses == []
        assert result.substitutes == []
        assert result.production is None
        assert result.inbound_po is None


class TestAdaptBackOrderMissingGatewayEvidence:
    def test_no_inventory_snapshot_returns_none(self):
        record = _record(original_event=_event())
        assert adapt_back_order(record) is None

    def test_missing_primary_dc_returns_none(self):
        snapshot = _inventory_snapshot()
        snapshot.pop("primary_dc")
        record = _record(
            original_event=_event(),
            enrichment_context={"inventory_snapshot": snapshot},
        )
        assert adapt_back_order(record) is None

    def test_missing_atp_date_returns_none(self):
        snapshot = _inventory_snapshot()
        snapshot.pop("atp_date")
        record = _record(
            original_event=_event(),
            enrichment_context={"inventory_snapshot": snapshot},
        )
        assert adapt_back_order(record) is None

    def test_missing_primary_dc_qty_returns_none(self):
        snapshot = _inventory_snapshot()
        snapshot["primary_dc"] = {"plant": "DC-EAST"}
        record = _record(
            original_event=_event(),
            enrichment_context={"inventory_snapshot": snapshot},
        )
        assert adapt_back_order(record) is None


class TestAdaptBackOrderSynthesisFallback:
    def test_synthesizes_recipe_output_when_resolution_data_empty(self):
        record = _record(
            original_event=_event(ordered=100, available=40),  # 60% gap → SEVERE
            enrichment_context={"inventory_snapshot": _inventory_snapshot()},
            resolution_data={},
        )
        result = adapt_back_order(record)
        assert result is not None
        assert result.gap_qty == 60.0
        assert result.at_risk == 600.0  # 60 × 10

    def test_invalid_ordered_qty_returns_none(self):
        record = _record(
            original_event=_event(ordered=0, available=0),
            enrichment_context={"inventory_snapshot": _inventory_snapshot()},
        )
        assert adapt_back_order(record) is None


class TestRegistryWiring:
    def test_back_order_recipe_registered(self):
        from api.analysis_adapters import ANALYSIS_ADAPTERS
        assert "BackOrderResolutionRecipe.py" in ANALYSIS_ADAPTERS
        field, _ = ANALYSIS_ADAPTERS["BackOrderResolutionRecipe.py"]
        assert field == "backorder_analysis"

    def test_back_order_intent_routes_to_recipe(self):
        from api.analysis_adapters import INTENT_TO_RECIPE_NAME
        assert INTENT_TO_RECIPE_NAME["BACK_ORDER"] == "BackOrderResolutionRecipe.py"
