from __future__ import annotations

import pytest
from contracts.models import GatewayResponse, GraphState, OrderEvent
from gateways.registry import clear_registry, register_gateway
from gateways.stub import StubGateway


@pytest.fixture
def pricing_event() -> GraphState:
    return GraphState(
        event=OrderEvent(
            order_id="SO-1001",
            line_item=1,
            po_price=90.0,
            sap_base_price=100.0,
            retailer_id="R-01",
            line_count=1,
        )
    )


@pytest.fixture
def credit_event() -> GraphState:
    return GraphState(
        event=OrderEvent(
            order_id="SO-2001",
            line_item=1,
            po_price=100.0,
            sap_base_price=100.0,
            requester_role="ORDER_MANAGER",
            credit_limit=10_000.0,
            current_exposure=10_100.0,
            line_count=1,
        )
    )


@pytest.fixture
def mass_event() -> GraphState:
    return GraphState(
        event=OrderEvent(
            order_id="SO-3001",
            line_item=1,
            po_price=70.0,
            sap_base_price=100.0,
            line_count=11,
        )
    )


@pytest.fixture(autouse=True)
def _register_oms_stub():
    """Register an OMS stub gateway for DuplicatePO resolution context.

    The DuplicatePORecipe declares gateway dependencies (oms/get_fulfillment_status,
    oms/get_matched_po_details) that are resolved by the resolve_dependencies node.
    This stub provides sensible defaults so end-to-end graph tests work without
    real OMS connectivity.
    """
    oms_stub = StubGateway(
        "oms",
        responses={
            "get_fulfillment_status": GatewayResponse(
                gateway_name="oms",
                operation="get_fulfillment_status",
                status="SUCCESS",
                data={"fulfilled": False},
            ),
            "get_inventory_snapshot": GatewayResponse(
                gateway_name="oms",
                operation="get_inventory_snapshot",
                status="SUCCESS",
                # Verdict Pillar 1 (T3): payload carries every
                # audit-bearing subfield required by
                # BackOrderAnalysisData (registry: primary_dc + atp_date
                # always, conditional alternate_warehouses /
                # substitutes / production / inbound_po).
                data={
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
                            "acceptance_rate": 0.85, "source": "catalog",
                            "priority": 1,
                        },
                    ],
                    "production": {"qty": 100.0, "date": "2026-05-05"},
                    "inbound_po": {"qty": 75.0, "eta": "2026-05-02", "po_num": "PO-INB-1"},
                },
            ),
            "get_matched_po_details": GatewayResponse(
                gateway_name="oms",
                operation="get_matched_po_details",
                status="SUCCESS",
                # Verdict Pillar 1 (T2): payload carries every
                # audit-bearing subfield required by
                # DuplicateDetectionData (registry: original_order +
                # duplicate_order OrderSnapshot pair, days_between,
                # cancellation_target). Recipe-input booleans
                # (has_revision_indicator, line_items_identical) are
                # alongside.
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
                        "so_number": "SO-DUP-001",
                        "po_number": "PO-4000",
                        "created_date": "2026-04-20",
                        "total_value": 1000.0,
                        "line_count": 1,
                        "status": "OPEN",
                        "lines": [
                            {"sku": "SKU-A", "description": "Widget", "qty": 10.0, "unit_price": 100.0},
                        ],
                    },
                    "duplicate_order": {
                        "so_number": "SO-DUP-002",
                        "po_number": "PO-4001",
                        "created_date": "2026-04-21",
                        "total_value": 1000.0,
                        "line_count": 1,
                        "status": "OPEN",
                        "lines": [
                            {"sku": "SKU-A", "description": "Widget", "qty": 10.0, "unit_price": 100.0},
                        ],
                    },
                },
            ),
        },
    )
    notification_stub = StubGateway(
        "buyer_notification",
        responses={
            "send": GatewayResponse(
                gateway_name="buyer_notification",
                operation="send",
                status="SUCCESS",
                data={"delivered": True},
            ),
        },
    )
    register_gateway(oms_stub)
    register_gateway(notification_stub)
    yield
    clear_registry()


@pytest.fixture
def duplicate_po_event() -> GraphState:
    return GraphState(
        event=OrderEvent(
            order_id="PO-4001",
            line_item=1,
            po_price=100.0,
            sap_base_price=100.0,
            event_type="EDI_850_DUPLICATE_PO",
            retailer_id="R-10",
            line_count=1,
            metadata={
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
                "matched_po_id": "PO-4000",
            },
        )
    )


@pytest.fixture
def duplicate_po_resend_event() -> GraphState:
    """EC04: exact PO resend (Costco-style) — high signals, timestamp lower."""
    return GraphState(
        event=OrderEvent(
            order_id="PO-88424",
            line_item=1,
            po_price=36.0,
            sap_base_price=36.0,
            event_type="EDI_850_DUPLICATE_PO",
            retailer_id="R-05",
            line_count=2,
            metadata={
                "signal_scores": {
                    "po_number": 1.0,
                    "customer_id": 1.0,
                    "line_items": 1.0,
                    "amount": 1.0,
                    "timestamp": 0.3,
                    "ship_to": 1.0,
                    "channel": 1.0,
                    "delivery_date": 1.0,
                },
            },
        )
    )


@pytest.fixture
def duplicate_po_batch_event() -> GraphState:
    """EC08: one PO from a multi-PO batch (Amazon-style) — no match → PASS."""
    return GraphState(
        event=OrderEvent(
            order_id="PO-AMZ-003",
            line_item=1,
            po_price=8.96,
            sap_base_price=8.96,
            event_type="EDI_850_DUPLICATE_PO",
            retailer_id="R-08",
            line_count=1,
            metadata={
                "signal_scores": {
                    "po_number": 0.0,
                    "customer_id": 1.0,
                    "line_items": 0.0,
                    "amount": 0.0,
                    "timestamp": 0.0,
                    "ship_to": 0.0,
                    "channel": 1.0,
                    "delivery_date": 0.0,
                },
                "source_email_id": "EC08-multiple-pos-amazon",
                "batch_po_index": 3,
            },
        )
    )
