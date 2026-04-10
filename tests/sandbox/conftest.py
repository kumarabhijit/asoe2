"""Sandbox test fixtures.

Provides shared fixtures for sandbox integration tests:
  - FastAPI TestClient with fresh store
  - JWT tokens for all RBAC roles
  - Helper to authenticate via the multi-step login flow
  - SQLite-backed exception store for DB persistence tests
  - InMemoryPubSub instance for WebSocket event tests
"""

from __future__ import annotations

import json
from typing import Dict

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.pubsub import InMemoryPubSub, event_publisher
from api.store import exception_store


# ---------------------------------------------------------------------------
# App / Client
# ---------------------------------------------------------------------------

@pytest.fixture()
def app():
    """Fresh FastAPI app per test."""
    return create_app()


@pytest.fixture()
def client(app):
    """TestClient with cleared exception store and pub/sub."""
    exception_store.clear()
    if hasattr(event_publisher, "clear"):
        event_publisher.clear()
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# JWT tokens for all RBAC roles (architecture_v3.md Section 11.2)
# ---------------------------------------------------------------------------

SANDBOX_TENANT = "sandbox-tenant"


@pytest.fixture()
def analyst_token():
    return create_test_token(
        sub="analyst-user", email="analyst@asoe.test",
        name="Analyst User", roles=["analyst"], org=SANDBOX_TENANT,
    )


@pytest.fixture()
def manager_token():
    return create_test_token(
        sub="manager-user", email="manager@asoe.test",
        name="Manager User", roles=["manager"], org=SANDBOX_TENANT,
    )


@pytest.fixture()
def admin_token():
    return create_test_token(
        sub="admin-user", email="admin@asoe.test",
        name="Admin User", roles=["admin"], org=SANDBOX_TENANT,
    )


@pytest.fixture()
def viewer_token():
    return create_test_token(
        sub="viewer-user", email="viewer@asoe.test",
        name="Viewer User", roles=["viewer"], org=SANDBOX_TENANT,
    )


@pytest.fixture()
def partner_token():
    return create_test_token(
        sub="partner-user", email="partner@asoe.test",
        name="Partner User", roles=["partner"], org=SANDBOX_TENANT,
        retailer_id="R-01",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth_header(token: str) -> Dict[str, str]:
    """Build Authorization header dict from a JWT token."""
    return {"Authorization": f"Bearer {token}"}


# Standard resolve request payloads for each intent
PRICING_EVENT = {
    "order_id": "SO-1001",
    "event_type": "EDI_850_PRICE_MISMATCH",
    "po_price": 90.0,
    "sap_base_price": 100.0,
    "retailer_id": "R-01",
    "line_count": 1,
}

CREDIT_BLOCK_EVENT = {
    "order_id": "SO-2001",
    "event_type": "EDI_850_CREDIT_BLOCK",
    "po_price": 100.0,
    "sap_base_price": 100.0,
    "requester_role": "ORDER_MANAGER",
    "credit_limit": 10000.0,
    "current_exposure": 10100.0,
    "line_count": 1,
}

MASS_PRICING_EVENT = {
    "order_id": "SO-3001",
    "event_type": "EDI_850_PRICE_MISMATCH",
    "po_price": 70.0,
    "sap_base_price": 100.0,
    "line_count": 15,
}

DUPLICATE_PO_EVENT = {
    "order_id": "PO-9001",
    "event_type": "EDI_850_DUPLICATE_PO",
    "po_price": 100.0,
    "sap_base_price": 100.0,
    "retailer_id": "R-04",
    "line_count": 1,
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
        "matched_po_id": "PO-9000",
    },
}


@pytest.fixture()
def pubsub():
    """Fresh InMemoryPubSub instance."""
    return InMemoryPubSub()
