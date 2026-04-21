"""End-to-end tests for the 5 OM-adjacent intents that reached backend
parity with asoe-ui Phase 8.10: BACK_ORDER, OVER_MAX, MIN_ORDER_QTY,
PALLET_CONFIG, DELIVERY_DELAY.

Covers for each intent:
  1. Health endpoint exposes the intent + recipe in allowed_intents /
     allowed_recipes.
  2. Resolve endpoint classifies, audits, executes per branch.
  3. Trace endpoint includes the new shadow policy_hits tags.
  4. Stats by_intent aggregation.

Mirrors test_e2e_other_intents.py in density; one class per intent.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.store import exception_store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
def client(app):
    exception_store.clear()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def analyst_token():
    return create_test_token(roles=["analyst"], org="tenant-a")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 0. Health endpoint — all 5 intents + recipes present
# ---------------------------------------------------------------------------

class TestHealthOmAdjacent:
    def test_intents_in_allowed_intents(self, client):
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        intents = set(r.json()["allowed_intents"])
        for expected in (
            "BACK_ORDER", "OVER_MAX", "MIN_ORDER_QTY",
            "PALLET_CONFIG", "DELIVERY_DELAY",
        ):
            assert expected in intents

    def test_recipes_in_allowed_recipes(self, client):
        r = client.get("/api/v1/health")
        recipes = set(r.json()["allowed_recipes"])
        for expected in (
            "BackOrderResolutionRecipe.py",
            "OverMaxTrimRecipe.py",
            "MOQRoundUpRecipe.py",
            "PalletAlignmentRecipe.py",
            "DeliveryDelayResolutionRecipe.py",
        ):
            assert expected in recipes


# ---------------------------------------------------------------------------
# 1. BACK_ORDER — resolve, branches, trace, stats
# ---------------------------------------------------------------------------

def _back_order_event(ordered: float, available: float) -> dict:
    return {
        "order_id": f"PO-BO-{int(ordered)}-{int(available)}",
        "line_item": 1,
        "po_price": 10.0,
        "sap_base_price": 10.0,
        "event_type": "BACK_ORDER_OOS",
        "retailer_id": "R-70",
        "line_count": 1,
        "sku": "SKU-BO-1",
        "metadata": {
            "ordered_qty": ordered,
            "available_qty": available,
            "unit_price": 10.0,
            "uom": "CS",
        },
    }


class TestResolveBackOrder:
    def test_minor_gap_review_required(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_back_order_event(100, 75),  # 25% gap
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["intent"] == "BACK_ORDER"
        assert data["shadow_verdict"] == "YELLOW"
        assert data["final_status"] == "MANUAL_REVIEW_REQUIRED"

    def test_severe_gap_blocked(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_back_order_event(100, 40),  # 60% gap
            headers=_auth(analyst_token),
        )
        data = r.json()
        assert data["intent"] == "BACK_ORDER"
        assert data["shadow_verdict"] == "RED"
        assert data["final_status"] == "BLOCKED"

    def test_trace_carries_policy_hit(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_back_order_event(100, 40),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]
        trace = client.get(
            f"/api/v1/exceptions/{exc_id}/trace", headers=_auth(analyst_token),
        ).json()
        assert "BACK_ORDER_SEVERE_GAP" in trace["shadow_policy_hits"]


# ---------------------------------------------------------------------------
# 2. OVER_MAX
# ---------------------------------------------------------------------------

def _over_max_event(total_ordered: float, max_qty: float) -> dict:
    return {
        "order_id": f"PO-OM-{int(total_ordered)}",
        "line_item": 1,
        "po_price": 10.0,
        "sap_base_price": 10.0,
        "event_type": "OVER_MAX_QTY",
        "retailer_id": "R-71",
        "line_count": 1,
        "metadata": {
            "total_ordered": total_ordered,
            "max_qty": max_qty,
            "order_lines": [
                {"sku": "A", "description": "A", "qty": total_ordered,
                 "max_line_qty": max_qty, "is_even_layer_item": True},
            ],
        },
    }


class TestResolveOverMax:
    def test_minor_exceedance_review_required(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_over_max_event(130, 100),  # 30% over
            headers=_auth(analyst_token),
        )
        data = r.json()
        assert data["intent"] == "OVER_MAX"
        assert data["shadow_verdict"] == "YELLOW"
        assert data["final_status"] == "MANUAL_REVIEW_REQUIRED"

    def test_severe_exceedance_blocked(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_over_max_event(170, 100),  # 70% over
            headers=_auth(analyst_token),
        )
        data = r.json()
        assert data["intent"] == "OVER_MAX"
        assert data["shadow_verdict"] == "RED"
        assert data["final_status"] == "BLOCKED"

    def test_trace_carries_policy_hit(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_over_max_event(170, 100),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]
        trace = client.get(
            f"/api/v1/exceptions/{exc_id}/trace", headers=_auth(analyst_token),
        ).json()
        assert "OVER_MAX_SEVERE_EXCEEDANCE" in trace["shadow_policy_hits"]


# ---------------------------------------------------------------------------
# 3. MIN_ORDER_QTY
# ---------------------------------------------------------------------------

def _moq_event(ordered: float, moq: float) -> dict:
    return {
        "order_id": f"PO-MOQ-{int(ordered)}",
        "line_item": 1,
        "po_price": 5.0,
        "sap_base_price": 5.0,
        "event_type": "MIN_ORDER_QTY",
        "retailer_id": "R-72",
        "line_count": 1,
        "sku": "SKU-MOQ-1",
        "metadata": {
            "ordered_qty": ordered,
            "moq_qty": moq,
            "unit_cost": 5.0,
            "uom": "CS",
        },
    }


class TestResolveMinOrderQty:
    def test_minor_shortfall_review_required(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_moq_event(40, 48),  # ~17% shortfall
            headers=_auth(analyst_token),
        )
        data = r.json()
        assert data["intent"] == "MIN_ORDER_QTY"
        assert data["shadow_verdict"] == "YELLOW"
        assert data["final_status"] == "MANUAL_REVIEW_REQUIRED"

    def test_severe_shortfall_blocked(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_moq_event(25, 48),  # ~48% shortfall
            headers=_auth(analyst_token),
        )
        data = r.json()
        assert data["intent"] == "MIN_ORDER_QTY"
        assert data["shadow_verdict"] == "RED"
        assert data["final_status"] == "BLOCKED"

    def test_trace_carries_policy_hit(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_moq_event(25, 48),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]
        trace = client.get(
            f"/api/v1/exceptions/{exc_id}/trace", headers=_auth(analyst_token),
        ).json()
        assert "MOQ_SEVERE_SHORTFALL" in trace["shadow_policy_hits"]


# ---------------------------------------------------------------------------
# 4. PALLET_CONFIG
# ---------------------------------------------------------------------------

def _pallet_event() -> dict:
    return {
        "order_id": "PO-PLT-001",
        "line_item": 1,
        "po_price": 10.0,
        "sap_base_price": 10.0,
        "event_type": "PALLET_CONFIG_VIOLATION",
        "retailer_id": "R-73",
        "line_count": 1,
        "metadata": {
            "pallet_lines": [
                # 100 cases at 24/layer 96/pallet → 1 pallet + 4 loose
                {"sku": "A", "description": "A", "layer_qty": 24,
                 "pallet_qty": 96, "ordered_qty": 100, "uom": "CS"},
            ],
        },
    }


class TestResolvePalletConfig:
    def test_violation_review_required(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_pallet_event(),
            headers=_auth(analyst_token),
        )
        data = r.json()
        assert data["intent"] == "PALLET_CONFIG"
        assert data["shadow_verdict"] == "YELLOW"
        assert data["final_status"] == "MANUAL_REVIEW_REQUIRED"

    def test_trace_carries_policy_hit(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_pallet_event(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]
        trace = client.get(
            f"/api/v1/exceptions/{exc_id}/trace", headers=_auth(analyst_token),
        ).json()
        assert "PALLET_CONFIG_REVIEW" in trace["shadow_policy_hits"]


# ---------------------------------------------------------------------------
# 5. DELIVERY_DELAY
# ---------------------------------------------------------------------------

def _delay_event(days_late: int) -> dict:
    return {
        "order_id": f"PO-DD-{days_late}",
        "line_item": 1,
        "po_price": 10.0,
        "sap_base_price": 10.0,
        "event_type": "DELIVERY_DELAY",
        "retailer_id": "R-74",
        "line_count": 1,
        "metadata": {
            "planned_date": "2026-04-20T00:00:00Z",
            "projected_eta": f"2026-04-{20 + days_late:02d}T00:00:00Z",
            "days_late": days_late,
            "delay_category": "CARRIER_DELAY",
        },
    }


class TestResolveDeliveryDelay:
    def test_minor_delay_review_required(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_delay_event(3),
            headers=_auth(analyst_token),
        )
        data = r.json()
        assert data["intent"] == "DELIVERY_DELAY"
        assert data["shadow_verdict"] == "YELLOW"
        assert data["final_status"] == "MANUAL_REVIEW_REQUIRED"

    def test_severe_delay_blocked(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_delay_event(7),
            headers=_auth(analyst_token),
        )
        data = r.json()
        assert data["intent"] == "DELIVERY_DELAY"
        assert data["shadow_verdict"] == "RED"
        assert data["final_status"] == "BLOCKED"

    def test_trace_carries_policy_hit(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_delay_event(7),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]
        trace = client.get(
            f"/api/v1/exceptions/{exc_id}/trace", headers=_auth(analyst_token),
        ).json()
        assert "DELIVERY_DELAY_SEVERE" in trace["shadow_policy_hits"]


# ---------------------------------------------------------------------------
# Stats — every new intent shows up in the by_intent aggregation
# ---------------------------------------------------------------------------

class TestStatsOmAdjacent:
    def test_all_five_intents_aggregate(self, client, analyst_token):
        # Produce one exception per intent to seed the by_intent
        # aggregation.
        for payload in (
            _back_order_event(100, 40),
            _over_max_event(130, 100),
            _moq_event(25, 48),
            _pallet_event(),
            _delay_event(7),
        ):
            client.post(
                "/api/v1/exceptions/resolve",
                json=payload, headers=_auth(analyst_token),
            )
        stats = client.get(
            "/api/v1/exceptions/stats", headers=_auth(analyst_token),
        ).json()
        for expected in (
            "BACK_ORDER", "OVER_MAX", "MIN_ORDER_QTY",
            "PALLET_CONFIG", "DELIVERY_DELAY",
        ):
            assert stats["by_intent"].get(expected, 0) >= 1
