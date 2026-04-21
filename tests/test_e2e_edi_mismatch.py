"""End-to-end tests for the EDI_MISMATCH use case.

Covers sub_type-level routing and the PRICE_MISMATCH classifier fork:

  1. Health endpoint serves EDI_MISMATCH + EdiMismatchRecipe.py.
  2. Each sub_type (SKU / QTY / UOM / SHIP_TO) produces its expected
     terminal status.
  3. PRICE_MISMATCH is re-routed at classifier time to
     CONTRACTUAL_CORRECTION — preserves PriceAdjustmentRecipe.py as the
     single source of truth for pricing (CLAUDE.md §1).
  4. Trace endpoint surfaces the new shadow policy_hits tags.
  5. Stats by_intent aggregation.
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


@pytest.fixture()
def manager_token():
    return create_test_token(roles=["manager"], org="tenant-a")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Sample EDI_850_LINE_MISMATCH events
# ---------------------------------------------------------------------------

def _line_mismatch_event(sub_type: str, *, order_suffix: str = "001") -> dict:
    return {
        "order_id": f"EDM-{sub_type}-{order_suffix}",
        "line_item": 1,
        "po_price": 100.0,
        "sap_base_price": 100.0,
        "event_type": "EDI_850_LINE_MISMATCH",
        "retailer_id": "R-50",
        "line_count": 1,
        "metadata": {
            "mismatch_sub_type": sub_type,
            "expected_value": "expected-x",
            "received_value": "received-y",
        },
    }


# ---------------------------------------------------------------------------
# 1. Health endpoint
# ---------------------------------------------------------------------------

class TestHealthEdiMismatch:
    def test_edi_mismatch_in_allowed_intents(self, client):
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        assert "EDI_MISMATCH" in r.json()["allowed_intents"]

    def test_recipe_in_allowed_recipes(self, client):
        r = client.get("/api/v1/health")
        assert "EdiMismatchRecipe.py" in r.json()["allowed_recipes"]


# ---------------------------------------------------------------------------
# 2. Resolve — per-sub_type branches
# ---------------------------------------------------------------------------

class TestResolveEdiMismatch:
    def test_sku_mismatch_red_blocked(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_line_mismatch_event("SKU_MISMATCH"),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["intent"] == "EDI_MISMATCH"
        assert data["shadow_verdict"] == "RED"
        assert data["final_status"] == "BLOCKED"

    def test_qty_mismatch_yellow_review(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_line_mismatch_event("QTY_MISMATCH"),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["intent"] == "EDI_MISMATCH"
        assert data["shadow_verdict"] == "YELLOW"
        assert data["final_status"] == "MANUAL_REVIEW_REQUIRED"

    def test_uom_mismatch_yellow_review(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_line_mismatch_event("UOM_MISMATCH"),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        assert r.json()["final_status"] == "MANUAL_REVIEW_REQUIRED"

    def test_ship_to_mismatch_yellow_review(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_line_mismatch_event("SHIP_TO_MISMATCH"),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["intent"] == "EDI_MISMATCH"
        assert data["shadow_verdict"] == "YELLOW"
        assert data["final_status"] == "MANUAL_REVIEW_REQUIRED"


# ---------------------------------------------------------------------------
# 3. PRICE_MISMATCH fork — must route to CONTRACTUAL_CORRECTION
# ---------------------------------------------------------------------------

class TestPriceMismatchRouting:
    def test_price_mismatch_classifies_as_contractual_correction(
        self, client, analyst_token,
    ):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_line_mismatch_event("PRICE_MISMATCH"),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()
        # The classifier re-routes PRICE_MISMATCH out of EDI_MISMATCH so
        # PriceAdjustmentRecipe.py remains the single source of truth for
        # pricing (CLAUDE.md §1).
        assert data["intent"] == "CONTRACTUAL_CORRECTION"
        assert data["selected_recipe"] == "PriceAdjustmentRecipe.py"

    def test_price_mismatch_does_not_run_edi_recipe(
        self, client, analyst_token,
    ):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_line_mismatch_event("PRICE_MISMATCH"),
            headers=_auth(analyst_token),
        )
        assert r.json()["selected_recipe"] != "EdiMismatchRecipe.py"


# ---------------------------------------------------------------------------
# 4. Trace endpoint shape — new shadow policy_hits
# ---------------------------------------------------------------------------

class TestTraceShapeEdiMismatch:
    def test_sku_mismatch_tag_present(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_line_mismatch_event("SKU_MISMATCH"),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]
        trace = client.get(
            f"/api/v1/exceptions/{exc_id}/trace",
            headers=_auth(analyst_token),
        )
        assert trace.status_code == 200
        assert "EDI_SKU_MISMATCH_HARD_REJECT" in trace.json()["shadow_policy_hits"]

    def test_qty_mismatch_tag_present(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_line_mismatch_event("QTY_MISMATCH"),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]
        trace = client.get(
            f"/api/v1/exceptions/{exc_id}/trace",
            headers=_auth(analyst_token),
        )
        assert "EDI_QTY_MISMATCH_REVIEW" in trace.json()["shadow_policy_hits"]

    def test_ship_to_tag_present(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_line_mismatch_event("SHIP_TO_MISMATCH"),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]
        trace = client.get(
            f"/api/v1/exceptions/{exc_id}/trace",
            headers=_auth(analyst_token),
        )
        assert "EDI_SHIP_TO_ESCALATE" in trace.json()["shadow_policy_hits"]


# ---------------------------------------------------------------------------
# 5. Stats aggregation including mixed queue
# ---------------------------------------------------------------------------

class TestStatsEdiMismatch:
    def test_by_intent_counts_edi_mismatch(self, client, analyst_token):
        for st in ("SKU_MISMATCH", "QTY_MISMATCH", "UOM_MISMATCH", "SHIP_TO_MISMATCH"):
            client.post(
                "/api/v1/exceptions/resolve",
                json=_line_mismatch_event(st, order_suffix=f"stat-{st}"),
                headers=_auth(analyst_token),
            )
        stats = client.get("/api/v1/exceptions/stats", headers=_auth(analyst_token)).json()
        assert stats["by_intent"].get("EDI_MISMATCH", 0) >= 4

    def test_price_mismatch_counts_as_contractual_correction(self, client, analyst_token):
        client.post(
            "/api/v1/exceptions/resolve",
            json=_line_mismatch_event("PRICE_MISMATCH", order_suffix="pm-stat"),
            headers=_auth(analyst_token),
        )
        stats = client.get("/api/v1/exceptions/stats", headers=_auth(analyst_token)).json()
        # Confirms the classifier fork landed the exception under the pricing
        # intent, not EDI_MISMATCH.
        assert stats["by_intent"].get("CONTRACTUAL_CORRECTION", 0) >= 1
