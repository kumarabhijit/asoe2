"""T4 adapter tests — PriceAdjustmentRecipe → PriceAnalysisData.

Source bags exercised:
  * `event` + `event.metadata` — po_price, sap_base_price, line_count,
    sku, uom.
  * `record.enrichment_context["sap_doc_context"]` — SAP doc metadata
    (audit-bearing: doc_type, doc_number).
  * `record.enrichment_context["contract_context"]` — contract lookup
    (audit-bearing: rule_id; contextual: contract_ref).
  * `record.enrichment_context["promotion_context"]` — promotion
    master (audit-bearing: root_cause_category; contextual:
    promotion_ref).

Invariants (post price_analysis_gateway_gap retirement):
  * Happy path: every audit-bearing field projects.
  * Missing doc_type / doc_number / rule_id / root_cause_category
    → adapter returns None → AUDIT_CONTEXT_MISSING.
  * contract_ref / promotion_ref absent → adapter still projects;
    those fields are contextual now.
"""

from __future__ import annotations

from api.analysis_adapters import adapt_price
from api.store import ExceptionRecord


def _record(
    *,
    enrichment_context=None,
    resolution_data=None,
    original_event=None,
    selected_recipe="PriceAdjustmentRecipe.py",
    intent="CONTRACTUAL_CORRECTION",
) -> ExceptionRecord:
    return ExceptionRecord(
        tenant_id="t1",
        order_id="PO-PRICE-1",
        event_type="EDI_850_PRICE_MISMATCH",
        trace_id="trace-1",
        intent=intent,
        selected_recipe=selected_recipe,
        resolution_data=resolution_data or {},
        original_event=original_event,
        enrichment_context=enrichment_context or {},
    )


def _event(
    *, po_price=100.0, sap_base_price=120.0, line_count=10, sku="SKU-A",
    metadata=None,
) -> dict:
    return {
        "order_id": "PO-PRICE-1",
        "line_item": 1,
        "po_price": po_price,
        "sap_base_price": sap_base_price,
        "line_count": line_count,
        "sku": sku,
        "metadata": metadata or {},
    }


def _gateway_bag(**overrides) -> dict:
    base = {
        "sap_doc_context": {
            "doc_type": "Sales Order",
            "doc_number": "5500001234",
            "uom": "CS",
            "material_desc": "Widget",
            "order_date": "2026-04-22",
        },
        "contract_context": {
            "contract_ref": "KONA-CN-1001",
            "rule_id": "SO-PRICE-001",
            "root_cause_category": "CONTRACT_PRICE_OVERRIDE",
        },
        "promotion_context": {
            "promotion_ref": "PRMO-2026-04",
            "root_cause_category": "PROMOTION_HONOR",
        },
    }
    for k, v in overrides.items():
        base[k] = v
    return base


class TestAdaptPriceHappyPath:
    def test_projects_every_audit_bearing_field(self):
        record = _record(
            original_event=_event(),
            enrichment_context=_gateway_bag(),
        )
        result = adapt_price(record)
        assert result is not None
        # Event-derived audit-bearing
        assert result.po_unit_price == 100.0
        assert result.erp_unit_price == 120.0
        assert result.variance_amount == 20.0
        assert result.variance_pct == round(20.0 / 120.0, 6)
        assert result.total_quantity == 10.0
        assert result.total_at_risk == 200.0
        assert result.uom == "CS"
        assert result.sku == "SKU-A"
        # Gateway audit-bearing
        assert result.doc_type == "Sales Order"
        assert result.doc_number == "5500001234"
        assert result.rule_id == "SO-PRICE-001"
        # Promotion takes precedence over contract for root_cause when present
        assert result.root_cause_category == "PROMOTION_HONOR"
        # Contextual
        assert result.contract_ref == "KONA-CN-1001"
        assert result.promotion_ref == "PRMO-2026-04"
        assert result.material_desc == "Widget"

    def test_falls_back_to_sap_doc_sku(self):
        event = _event(sku=None)
        event["sku"] = None
        record = _record(
            original_event=event,
            enrichment_context=_gateway_bag(
                sap_doc_context={
                    "doc_type": "Sales Order",
                    "doc_number": "5500001234",
                    "sku": "SKU-FROM-SAP",
                    "uom": "CS",
                },
            ),
        )
        result = adapt_price(record)
        assert result is not None
        assert result.sku == "SKU-FROM-SAP"


class TestAdaptPriceMissingGatewayEvidence:
    def test_no_enrichment_context_returns_none(self):
        record = _record(original_event=_event())
        assert adapt_price(record) is None

    def test_missing_doc_type_returns_none(self):
        bag = _gateway_bag()
        bag["sap_doc_context"].pop("doc_type")
        record = _record(original_event=_event(), enrichment_context=bag)
        assert adapt_price(record) is None

    def test_missing_doc_number_returns_none(self):
        bag = _gateway_bag()
        bag["sap_doc_context"].pop("doc_number")
        record = _record(original_event=_event(), enrichment_context=bag)
        assert adapt_price(record) is None

    def test_missing_rule_id_returns_none(self):
        bag = _gateway_bag()
        bag["contract_context"].pop("rule_id")
        record = _record(original_event=_event(), enrichment_context=bag)
        assert adapt_price(record) is None

    def test_missing_root_cause_category_returns_none(self):
        bag = _gateway_bag()
        bag["contract_context"].pop("root_cause_category")
        bag["promotion_context"].pop("root_cause_category")
        record = _record(original_event=_event(), enrichment_context=bag)
        assert adapt_price(record) is None


class TestAdaptPriceContextualFields:
    def test_missing_contract_ref_still_projects(self):
        bag = _gateway_bag()
        bag["contract_context"].pop("contract_ref")
        record = _record(original_event=_event(), enrichment_context=bag)
        result = adapt_price(record)
        assert result is not None
        assert result.contract_ref is None

    def test_missing_promotion_ref_still_projects(self):
        bag = _gateway_bag()
        bag["promotion_context"].pop("promotion_ref")
        record = _record(original_event=_event(), enrichment_context=bag)
        result = adapt_price(record)
        assert result is not None
        assert result.promotion_ref is None

    def test_root_cause_falls_back_to_contract_when_promotion_absent(self):
        bag = _gateway_bag()
        bag["promotion_context"].pop("root_cause_category")
        record = _record(original_event=_event(), enrichment_context=bag)
        result = adapt_price(record)
        assert result is not None
        assert result.root_cause_category == "CONTRACT_PRICE_OVERRIDE"


class TestAdaptPriceInvalidEvent:
    def test_zero_sap_base_price_returns_none(self):
        record = _record(
            original_event=_event(sap_base_price=0.0),
            enrichment_context=_gateway_bag(),
        )
        assert adapt_price(record) is None


class TestRegistryWiring:
    def test_price_recipe_registered_as_primary(self):
        from api.analysis_adapters import ANALYSIS_ADAPTERS
        assert "PriceAdjustmentRecipe.py" in ANALYSIS_ADAPTERS
        field, _ = ANALYSIS_ADAPTERS["PriceAdjustmentRecipe.py"]
        assert field == "price_analysis"

    def test_contractual_correction_intent_routes_to_recipe(self):
        from api.analysis_adapters import INTENT_TO_RECIPE_NAME
        assert INTENT_TO_RECIPE_NAME["CONTRACTUAL_CORRECTION"] == "PriceAdjustmentRecipe.py"

    def test_grandfather_clause_retired(self):
        """price_analysis_gateway_gap is removed from the registry."""
        import yaml
        from pathlib import Path
        registry_path = Path("compliance/audit_bearing_registry.yaml")
        data = yaml.safe_load(registry_path.read_text())
        clauses = data.get("grandfather_clauses") or {}
        assert "price_analysis_gateway_gap" not in clauses
