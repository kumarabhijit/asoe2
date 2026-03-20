from __future__ import annotations

import pytest
from contracts.models import GraphState, OrderEvent


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
            },
        )
    )
