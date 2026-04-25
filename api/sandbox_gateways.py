"""Sandbox-only StubGateway registration.

The live FastAPI server has no real SAP / OMS / SLA-contract
integrations wired (those are platform-team work, separate from this
repo). For local end-to-end runs we register the same stubs that
`tests/conftest.py` uses so the recipes' GatewayDependency entries
resolve and the audit-bearing fields populate downstream.

Mounted only when ASOE_ENV=sandbox (the default). Idempotent —
clear_registry() is called first so re-running the app on a hot
reload starts clean.

Real-environment behavior is identical: the recipes declare what they
need; whoever runs the server is responsible for wiring the real
gateway adapters at startup. This module is the sandbox shim.
"""

from __future__ import annotations

import os

from contracts.models import GatewayResponse
from gateways.registry import clear_registry, register_gateway
from gateways.stub import StubGateway


def register_sandbox_gateways() -> None:
    """Register every stub gateway needed for end-to-end runs.

    Mirrors tests/conftest.py so live-server runs match the test
    pipeline. Safe to call multiple times — clears the registry
    first.
    """
    if os.getenv("ASOE_ENV", "production").lower() != "sandbox":
        return

    clear_registry()

    register_gateway(StubGateway(
        "oms",
        responses={
            "get_fulfillment_status": GatewayResponse(
                gateway_name="oms", operation="get_fulfillment_status",
                status="SUCCESS", data={"fulfilled": False},
            ),
            "get_inventory_snapshot": GatewayResponse(
                gateway_name="oms", operation="get_inventory_snapshot",
                status="SUCCESS",
                data={
                    "primary_dc": {
                        "plant": "DC-EAST", "name": "East DC",
                        "region": "US-EAST", "qty": 50.0,
                    },
                    "atp_date": "2026-04-30",
                    "alternate_warehouses": [{
                        "plant": "DC-WEST", "name": "West DC",
                        "region": "US-WEST", "qty": 200.0,
                        "eta_days": 4, "freight_delta_per_unit": 0.50,
                        "freight_delta_total": 25.0,
                    }],
                    "substitutes": [{
                        "sku": "SKU-BO-1-ALT", "description": "Equivalent SKU",
                        "available_qty": 150.0, "price_delta_pct": 0.02,
                        "acceptance_rate": 0.85, "source": "catalog",
                        "priority": 1,
                    }],
                    "production": {"qty": 100.0, "date": "2026-05-05"},
                    "inbound_po": {"qty": 75.0, "eta": "2026-05-02", "po_num": "PO-INB-1"},
                },
            ),
            "get_matched_po_details": GatewayResponse(
                gateway_name="oms", operation="get_matched_po_details",
                status="SUCCESS",
                data={
                    "has_revision_indicator": False,
                    "line_items_identical": True,
                    "days_between": 1,
                    "cancellation_target": "SO-DUP-002",
                    "detection_method": "po_number+customer+lines",
                    "customer_id": "R-10",
                    "matching_fields": ["po_number", "customer_id", "line_items"],
                    "differing_fields": [],
                    "original_order": {
                        "so_number": "SO-DUP-001", "po_number": "PO-4000",
                        "created_date": "2026-04-20", "total_value": 1000.0,
                        "line_count": 1, "status": "OPEN",
                        "lines": [{"sku": "SKU-A", "description": "Widget",
                                   "qty": 10.0, "unit_price": 100.0}],
                    },
                    "duplicate_order": {
                        "so_number": "SO-DUP-002", "po_number": "PO-4001",
                        "created_date": "2026-04-21", "total_value": 1000.0,
                        "line_count": 1, "status": "OPEN",
                        "lines": [{"sku": "SKU-A", "description": "Widget",
                                   "qty": 10.0, "unit_price": 100.0}],
                    },
                },
            ),
            "get_price_hold_status": GatewayResponse(
                gateway_name="oms", operation="get_price_hold_status",
                status="SUCCESS", data={"status": "HELD"},
            ),
        },
    ))

    register_gateway(StubGateway(
        "buyer_notification",
        responses={
            "send": GatewayResponse(
                gateway_name="buyer_notification", operation="send",
                status="SUCCESS", data={"delivered": True},
            ),
        },
    ))

    register_gateway(StubGateway(
        "sap_doc",
        responses={
            "lookup": GatewayResponse(
                gateway_name="sap_doc", operation="lookup",
                status="SUCCESS",
                data={
                    "doc_type": "Sales Order", "doc_number": "5500001234",
                    "applied_condition_chain": ["PR00", "K007"],
                    "sku": "SKU-STUB-1", "uom": "CS",
                    "material_desc": "Stub Material",
                    "order_date": "2026-04-22",
                },
            ),
        },
    ))

    register_gateway(StubGateway(
        "sap_contract",
        responses={
            "lookup": GatewayResponse(
                gateway_name="sap_contract", operation="lookup",
                status="SUCCESS",
                data={
                    "contract_ref": "KONA-CN-1001",
                    "rule_id": "SO-PRICE-001",
                    "root_cause_category": "CONTRACT_PRICE_OVERRIDE",
                },
            ),
        },
    ))

    register_gateway(StubGateway(
        "promotion",
        responses={
            "lookup": GatewayResponse(
                gateway_name="promotion", operation="lookup",
                status="SUCCESS",
                data={
                    "promotion_ref": "PRMO-2026-04-Q2",
                    "root_cause_category": "PROMOTION_HONOR",
                },
            ),
        },
    ))

    register_gateway(StubGateway(
        "sap_block",
        responses={
            "lookup": GatewayResponse(
                gateway_name="sap_block", operation="lookup",
                status="SUCCESS",
                data={
                    "block_status": "ACTIVE",
                    "block_reason": "OVER_MAX_QTY",
                    "block_message": "Order exceeds contractual maximum",
                },
            ),
        },
    ))

    register_gateway(StubGateway(
        "sap_customer_master",
        responses={
            "lookup": GatewayResponse(
                gateway_name="sap_customer_master", operation="lookup",
                status="SUCCESS",
                data={"moq_source": "KNMT-MINBM", "channel": "DIRECT"},
            ),
        },
    ))

    register_gateway(StubGateway(
        "sla_contract",
        responses={
            "lookup": GatewayResponse(
                gateway_name="sla_contract", operation="lookup",
                status="SUCCESS",
                data={
                    "sla_deadline": "2026-04-25T00:00:00Z",
                    "at_risk": 1500.0,
                },
            ),
        },
    ))
