"""T5 adapter tests — extensions to delivery_delay / overmax / moq
adapters that retire the 3 grandfather clauses
(delivery_delay_financial_gap, overmax_gateway_gap, moq_gateway_gap).

The previously-grandfathered fields now project from gateway result
keys in `record.enrichment_context`:
  * delivery_delay: sla_contract_context → at_risk + sla_deadline.
  * overmax: contract_context + block_context → contract_ref +
    block_status + block_reason.
  * moq: customer_master_context + contract_context + block_context →
    moq_source + channel + contract_ref + block_status.

Each adapter falls back to event metadata when the gateway result key
is absent (shadow-gated paths) — preserved as a backward-compatible
shim for the case where shadow blocks before resolve_dependencies runs.
"""

from __future__ import annotations

from api.analysis_adapters import (
    adapt_delivery_delay,
    adapt_moq,
    adapt_overmax,
)
from api.store import ChildCase


def _record(
    *, recipe, intent, enrichment_context=None, resolution_data=None,
    original_event=None,
) -> ChildCase:
    return ChildCase(
        tenant_id="t1",
        order_id="ORD-T5",
        event_type="GENERIC",
        trace_id="trace-1",
        intent=intent,
        selected_recipe=recipe,
        resolution_data=resolution_data or {},
        original_event=original_event,
        enrichment_context=enrichment_context or {},
    )


# ─── delivery_delay ──────────────────────────────────────────────────────


class TestAdaptDeliveryDelayWithSlaGateway:
    def _event(self):
        return {
            "order_id": "PO-DD-1",
            "line_count": 2,
            "metadata": {
                "planned_date": "2026-04-20T00:00:00Z",
                "projected_eta": "2026-04-23T00:00:00Z",
                "delay_category": "CARRIER_DELAY",
            },
        }

    def test_at_risk_and_sla_deadline_project_from_sla_gateway(self):
        record = _record(
            recipe="DeliveryDelayResolutionRecipe.py",
            intent="DELIVERY_DELAY",
            original_event=self._event(),
            enrichment_context={
                "sla_contract_context": {
                    "at_risk": 1500.0,
                    "sla_deadline": "2026-04-25T00:00:00Z",
                },
            },
        )
        result = adapt_delivery_delay(record)
        assert result is not None
        assert result.at_risk == 1500.0
        assert result.sla_deadline == "2026-04-25T00:00:00Z"

    def test_falls_back_to_metadata_when_gateway_absent(self):
        event = self._event()
        event["metadata"]["at_risk"] = 999.0
        event["metadata"]["sla_deadline"] = "2026-04-26"
        record = _record(
            recipe="DeliveryDelayResolutionRecipe.py",
            intent="DELIVERY_DELAY",
            original_event=event,
        )
        result = adapt_delivery_delay(record)
        assert result is not None
        # Backward-compat: shadow-gated paths can still surface
        # metadata-supplied at_risk / sla_deadline.
        assert result.sla_deadline == "2026-04-26"


# ─── overmax ─────────────────────────────────────────────────────────────


class TestAdaptOverMaxWithSapGateways:
    def _event(self):
        return {
            "order_id": "PO-OM-1",
            "metadata": {
                "total_ordered": 130.0,
                "max_qty": 100.0,
                "uom": "CASE",
                "order_lines": [
                    {"sku": "A", "description": "A", "qty": 130.0,
                     "max_line_qty": 100.0, "is_even_layer_item": True},
                ],
            },
        }

    def test_contract_and_block_project_from_gateway(self):
        record = _record(
            recipe="OverMaxTrimRecipe.py", intent="OVER_MAX",
            original_event=self._event(),
            enrichment_context={
                "contract_context": {"contract_ref": "KONA-OM-100"},
                "block_context": {
                    "block_status": "ACTIVE",
                    "block_reason": "OVER_MAX_QTY",
                },
            },
        )
        result = adapt_overmax(record)
        assert result is not None
        assert result.contract_ref == "KONA-OM-100"
        assert result.block_status == "ACTIVE"
        assert result.block_reason == "OVER_MAX_QTY"

    def test_falls_back_to_metadata_when_gateway_absent(self):
        event = self._event()
        event["metadata"]["contract_ref"] = "META-CN-1"
        event["metadata"]["block_status"] = "META-BLK"
        record = _record(
            recipe="OverMaxTrimRecipe.py", intent="OVER_MAX",
            original_event=event,
        )
        result = adapt_overmax(record)
        assert result is not None
        assert result.contract_ref == "META-CN-1"
        assert result.block_status == "META-BLK"


# ─── moq ─────────────────────────────────────────────────────────────────


class TestAdaptMOQWithSapGateways:
    def _event(self):
        return {
            "order_id": "PO-MOQ-1",
            "sku": "SKU-MOQ-1",
            "metadata": {
                "ordered_qty": 18.0,
                "moq_qty": 20.0,
                "unit_cost": 12.5,
                "uom": "CS",
            },
        }

    def test_moq_source_channel_contract_block_project_from_gateway(self):
        record = _record(
            recipe="MOQRoundUpRecipe.py", intent="MIN_ORDER_QTY",
            original_event=self._event(),
            enrichment_context={
                "customer_master_context": {
                    "moq_source": "KNMT-MINBM",
                    "channel": "DIRECT",
                },
                "contract_context": {"contract_ref": "KONA-MQ-001"},
                "block_context": {"block_status": "ACTIVE"},
            },
        )
        result = adapt_moq(record)
        assert result is not None
        assert result.moq_source == "KNMT-MINBM"
        assert result.channel == "DIRECT"
        assert result.contract_ref == "KONA-MQ-001"
        assert result.block_status == "ACTIVE"

    def test_falls_back_to_metadata_when_gateway_absent(self):
        event = self._event()
        event["metadata"]["moq_source"] = "META-SRC"
        event["metadata"]["channel"] = "META-CH"
        event["metadata"]["contract_ref"] = "META-CN"
        event["metadata"]["block_status"] = "META-BLK"
        record = _record(
            recipe="MOQRoundUpRecipe.py", intent="MIN_ORDER_QTY",
            original_event=event,
        )
        result = adapt_moq(record)
        assert result is not None
        assert result.moq_source == "META-SRC"
        assert result.channel == "META-CH"


# ─── registry retirement assertions ──────────────────────────────────────


class TestClauseRetirement:
    def test_all_three_clauses_retired(self):
        import yaml
        from pathlib import Path
        data = yaml.safe_load(
            Path("compliance/audit_bearing_registry.yaml").read_text()
        )
        clauses = data.get("grandfather_clauses") or {}
        assert "delivery_delay_financial_gap" not in clauses
        assert "overmax_gateway_gap" not in clauses
        assert "moq_gateway_gap" not in clauses
        # T4 retirement also gone.
        assert "price_analysis_gateway_gap" not in clauses
        # No active clauses remain in this engagement.
        assert clauses == {}

    def test_recipes_have_t5_dependencies(self):
        from recipes.registry import REGISTRY

        dd = REGISTRY["DeliveryDelayResolutionRecipe.py"]
        assert any(d.gateway_name == "sla_contract" for d in dd.dependencies)

        om = REGISTRY["OverMaxTrimRecipe.py"]
        gw_names = {d.gateway_name for d in om.dependencies}
        assert "sap_contract" in gw_names
        assert "sap_block" in gw_names

        moq = REGISTRY["MOQRoundUpRecipe.py"]
        gw_names = {d.gateway_name for d in moq.dependencies}
        assert "sap_customer_master" in gw_names
        assert "sap_contract" in gw_names
        assert "sap_block" in gw_names
