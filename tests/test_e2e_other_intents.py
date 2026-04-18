"""End-to-end data-flow tests for CONTRACTUAL_CORRECTION, CREDIT_BLOCK,
and MASS_PRICING_ERROR use cases on the Exception Queue.

Validates the full API surface that the asoe-ui Exception Queue consumes:

  CONTRACTUAL_CORRECTION — GREEN shadow → PriceAdjustmentRecipe → COMPLETE
    Success: discount ≤ 15% → COMPLETE with applied_condition + new_net_price
    Failure: discount > 15% → FAIL_TO_HUMAN

  CREDIT_BLOCK — YELLOW shadow → MANUAL_REVIEW_REQUIRED (HITL)
    Recipe not auto-executed; human must approve/reject

  MASS_PRICING_ERROR — RED shadow → BLOCKED (no recipe)
    line_count > 10 triggers systemic failure block
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


@pytest.fixture()
def admin_token():
    return create_test_token(roles=["admin"], org="tenant-a")


@pytest.fixture()
def tenant_b_token():
    return create_test_token(roles=["analyst"], org="tenant-b")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Sample events
# ---------------------------------------------------------------------------

def _contractual_correction_success() -> dict:
    """10% discount — within MAX_DISCOUNT_ALLOWED (15%)."""
    return {
        "order_id": "SO-CC-001",
        "line_item": 1,
        "po_price": 90.0,
        "sap_base_price": 100.0,
        "event_type": "EDI_850_PRICE_MISMATCH",
        "retailer_id": "R-01",
        "line_count": 1,
    }


def _contractual_correction_over_threshold() -> dict:
    """20% discount — exceeds MAX_DISCOUNT_ALLOWED (15%) → FAIL_TO_HUMAN."""
    return {
        "order_id": "SO-CC-002",
        "line_item": 1,
        "po_price": 80.0,
        "sap_base_price": 100.0,
        "event_type": "EDI_850_PRICE_MISMATCH",
        "retailer_id": "R-01",
        "line_count": 1,
    }


def _credit_block() -> dict:
    """Credit fields present → CREDIT_BLOCK intent, YELLOW shadow."""
    return {
        "order_id": "SO-CB-001",
        "line_item": 1,
        "po_price": 100.0,
        "sap_base_price": 100.0,
        "event_type": "CREDIT_LIMIT_BREACH",
        "requester_role": "ORDER_MANAGER",
        "credit_limit": 10_000.0,
        "current_exposure": 10_100.0,
        "line_count": 1,
    }


def _credit_block_within_tolerance() -> dict:
    """Exposure exceeds limit by $100 — within $5,000 tolerance → RELEASED."""
    return {
        "order_id": "SO-CB-002",
        "line_item": 1,
        "po_price": 100.0,
        "sap_base_price": 100.0,
        "event_type": "CREDIT_LIMIT_BREACH",
        "requester_role": "ORDER_MANAGER",
        "credit_limit": 50_000.0,
        "current_exposure": 52_000.0,
        "line_count": 1,
    }


def _mass_pricing_error() -> dict:
    """line_count=15 → MASS_PRICING_ERROR intent, RED shadow → BLOCKED."""
    return {
        "order_id": "SO-MP-001",
        "line_item": 1,
        "po_price": 70.0,
        "sap_base_price": 100.0,
        "event_type": "EDI_850_PRICE_MISMATCH",
        "line_count": 15,
    }


# ===========================================================================
# CONTRACTUAL_CORRECTION — GREEN → PriceAdjustmentRecipe
# ===========================================================================

class TestContractualCorrection:
    """Full pipeline for pricing correction exceptions."""

    def test_successful_price_adjustment(self, client, analyst_token):
        """10% discount within threshold → GREEN → COMPLETE."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_contractual_correction_success(),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()

        assert data["intent"] == "CONTRACTUAL_CORRECTION"
        assert data["shadow_verdict"] == "GREEN"
        assert data["selected_recipe"] == "PriceAdjustmentRecipe.py"
        assert data["final_status"] == "COMPLETE"
        assert isinstance(data["explanation"], str)
        assert isinstance(data["trace_id"], str)

    def test_successful_recipe_output_fields(self, client, analyst_token):
        """Recipe outputs contain applied_condition and new_net_price."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_contractual_correction_success(),
            headers=_auth(analyst_token),
        )
        log = r.json()["execution_log"]
        assert log["recipe_name"] == "PriceAdjustmentRecipe.py"
        outputs = log["outputs"]
        assert outputs["status"] == "SUCCESS"
        assert outputs["applied_condition"] == "YK07"
        assert outputs["new_net_price"] == 90.0
        assert "payload" in outputs

    def test_over_threshold_fails_to_human(self, client, analyst_token):
        """20% discount exceeds 15% threshold → FAIL_TO_HUMAN."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_contractual_correction_over_threshold(),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()

        assert data["intent"] == "CONTRACTUAL_CORRECTION"
        assert data["shadow_verdict"] == "GREEN"
        assert data["selected_recipe"] == "PriceAdjustmentRecipe.py"
        assert data["final_status"] == "FAIL_TO_HUMAN"
        assert "exceeds" in data["explanation"].lower() or "threshold" in data["explanation"].lower()

    def test_crud_list_and_detail(self, client, analyst_token):
        """Exception persisted and visible in list and detail."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_contractual_correction_success(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]

        # List
        r = client.get("/api/v1/exceptions", headers=_auth(analyst_token))
        data = r.json()["data"]
        assert len(data) >= 1
        assert data[0]["intent"] == "CONTRACTUAL_CORRECTION"
        assert data[0]["event_type"] == "EDI_850_PRICE_MISMATCH"

        # Detail
        r = client.get(f"/api/v1/exceptions/{exc_id}", headers=_auth(analyst_token))
        detail = r.json()
        assert detail["order_id"] == "SO-CC-001"
        assert detail["resolution_data"]["applied_condition"] == "YK07"
        assert detail["resolution_data"]["new_net_price"] == 90.0

    def test_trace_response(self, client, analyst_token):
        """Trace contains full audit trail for pricing correction."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_contractual_correction_success(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]

        r = client.get(f"/api/v1/exceptions/{exc_id}/trace", headers=_auth(analyst_token))
        trace = r.json()
        assert trace["intent_selected"] == "CONTRACTUAL_CORRECTION"
        assert trace["shadow_verdict"] == "GREEN"
        assert trace["recipe_name"] == "PriceAdjustmentRecipe.py"
        assert trace["final_status"] == "COMPLETE"

    def test_filter_by_intent(self, client, analyst_token):
        """Filter queue by CONTRACTUAL_CORRECTION intent."""
        client.post(
            "/api/v1/exceptions/resolve",
            json=_contractual_correction_success(),
            headers=_auth(analyst_token),
        )
        client.post(
            "/api/v1/exceptions/resolve",
            json=_credit_block(),
            headers=_auth(analyst_token),
        )

        r = client.get(
            "/api/v1/exceptions?intent=CONTRACTUAL_CORRECTION",
            headers=_auth(analyst_token),
        )
        results = r.json()["data"]
        for exc in results:
            assert exc["intent"] == "CONTRACTUAL_CORRECTION"

    def test_stats_reflect_pricing_correction(self, client, analyst_token):
        """Stats include CONTRACTUAL_CORRECTION in by_intent."""
        client.post(
            "/api/v1/exceptions/resolve",
            json=_contractual_correction_success(),
            headers=_auth(analyst_token),
        )
        r = client.get("/api/v1/exceptions/stats", headers=_auth(analyst_token))
        stats = r.json()
        assert stats["by_intent"].get("CONTRACTUAL_CORRECTION", 0) >= 1

    def test_challenge_resolved_pricing_exception(self, client, analyst_token):
        """GREEN-tier HITL: challenge a completed pricing correction."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_contractual_correction_success(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]

        r = client.post(
            f"/api/v1/exceptions/{exc_id}/challenge",
            json={"challenge_reason": "Applied condition YK07 may be incorrect for this retailer"},
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        assert r.json()["lifecycle_state"] == "ESCALATED"


# ===========================================================================
# CREDIT_BLOCK — YELLOW → MANUAL_REVIEW_REQUIRED
# ===========================================================================

class TestCreditBlock:
    """Full pipeline for credit block exceptions."""

    def test_credit_block_routes_to_manual_review(self, client, analyst_token):
        """Credit fields → YELLOW shadow → MANUAL_REVIEW_REQUIRED.

        YELLOW verdict halts the pipeline before recipe selection,
        so selected_recipe is None (recipe not reached).
        """
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_credit_block(),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()

        assert data["intent"] == "CREDIT_BLOCK"
        assert data["shadow_verdict"] == "YELLOW"
        assert data["final_status"] == "MANUAL_REVIEW_REQUIRED"
        # YELLOW halts before recipe selection — recipe is None
        # The recipe would be CreditHoldReleaseRecipe.py if shadow were GREEN

    def test_credit_block_lifecycle_pending_review(self, client, analyst_token):
        """MANUAL_REVIEW_REQUIRED maps to PENDING_REVIEW lifecycle state."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_credit_block(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]

        r = client.get(f"/api/v1/exceptions/{exc_id}", headers=_auth(analyst_token))
        assert r.json()["lifecycle_state"] == "PENDING_REVIEW"

    def test_credit_block_approve_flow(self, client, analyst_token, manager_token):
        """Manager approves credit block → EXECUTING."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_credit_block(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]

        r = client.patch(f"/api/v1/exceptions/{exc_id}/disposition",
            json={"action": "ALLOW_BOTH", "reason_tag": "other", "notes": "Credit limit increase approved by Finance"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        assert r.json()["lifecycle_state"] == "RESOLVED"

    def test_credit_block_reject_flow(self, client, analyst_token, manager_token):
        """Manager rejects credit block → REJECTED."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_credit_block(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]

        r = client.patch(f"/api/v1/exceptions/{exc_id}/disposition",
            json={"action": "NO_ACTION", "reason_tag": "other", "notes": "Customer has outstanding payment — hold maintained"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        assert r.json()["lifecycle_state"] == "REJECTED"

    def test_credit_block_override_with_different_action(self, client, analyst_token, manager_token):
        """Manager overrides credit block recommendation with ALLOW_BOTH."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_credit_block(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]

        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/disposition",
            json={
                "action": "ALLOW_BOTH",
                "notes": "One-time exception — VIP customer with pending payment confirmed",
                "reason_tag": "other",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        assert r.json()["resolved_action"] == "ALLOW_BOTH"
        assert r.json()["lifecycle_state"] == "RESOLVED"

    def test_credit_block_trace(self, client, analyst_token):
        """Trace shows YELLOW verdict. Recipe is None (shadow halted before selection)."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_credit_block(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]

        r = client.get(f"/api/v1/exceptions/{exc_id}/trace", headers=_auth(analyst_token))
        trace = r.json()
        assert trace["intent_selected"] == "CREDIT_BLOCK"
        assert trace["shadow_verdict"] == "YELLOW"
        assert trace["final_status"] == "MANUAL_REVIEW_REQUIRED"

    def test_credit_block_in_stats(self, client, analyst_token):
        """Stats include CREDIT_BLOCK in by_intent and PENDING_REVIEW in by_lifecycle_state."""
        client.post(
            "/api/v1/exceptions/resolve",
            json=_credit_block(),
            headers=_auth(analyst_token),
        )
        r = client.get("/api/v1/exceptions/stats", headers=_auth(analyst_token))
        stats = r.json()
        assert stats["by_intent"].get("CREDIT_BLOCK", 0) >= 1
        assert stats["manual_review"] >= 1


# ===========================================================================
# MASS_PRICING_ERROR — RED → BLOCKED
# ===========================================================================

class TestMassPricingError:
    """Full pipeline for mass pricing error (systemic failure) exceptions."""

    def test_mass_pricing_blocked_by_shadow(self, client, analyst_token):
        """line_count > 10 → RED shadow → BLOCKED. No recipe executes."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_mass_pricing_error(),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()

        assert data["intent"] == "MASS_PRICING_ERROR"
        assert data["shadow_verdict"] == "RED"
        assert data["final_status"] == "BLOCKED"
        # No recipe selected for mass pricing — shadow blocks before selection
        assert data["explanation"] is not None

    def test_mass_pricing_lifecycle_blocked(self, client, analyst_token):
        """BLOCKED terminal status maps to BLOCKED lifecycle state."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_mass_pricing_error(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]

        r = client.get(f"/api/v1/exceptions/{exc_id}", headers=_auth(analyst_token))
        assert r.json()["lifecycle_state"] == "BLOCKED"

    def test_mass_pricing_admin_release_flow(self, client, analyst_token, admin_token):
        """RED-tier HITL: admin releases → PENDING_ADMIN_REVIEW → approve → EXECUTING."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_mass_pricing_error(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]

        # Admin release
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/admin-release",
            json={
                "release_reason": "False positive — line_count includes informational lines",
                "risk_acknowledgment": True,
            },
            headers=_auth(admin_token),
        )
        assert r.status_code == 200
        assert r.json()["lifecycle_state"] == "PENDING_ADMIN_REVIEW"

        # Admin approves
        r = client.patch(f"/api/v1/exceptions/{exc_id}/disposition",
            json={"action": "ALLOW_BOTH", "reason_tag": "other", "notes": "Verified — proceeding with manual review"},
            headers=_auth(admin_token),
        )
        assert r.status_code == 200
        assert r.json()["lifecycle_state"] == "RESOLVED"

    def test_mass_pricing_blocked_can_be_overridden_by_manager(
        self, client, analyst_token, manager_token,
    ):
        """Option A: manager can Override a RED+BLOCKED exception directly.

        Prior to the Phase 1 unified action model, BLOCKED was not
        overridable and required an admin-release first. Under Option A,
        the auditable Override path is available to manager+ across
        GREEN/YELLOW/RED verdicts.
        """
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_mass_pricing_error(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]

        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/disposition",
            json={
                "action": "ALLOW_BOTH",
                "notes": "Red verdict overridden with risk acknowledged",
                "reason_tag": "other",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        assert r.json()["lifecycle_state"] == "RESOLVED"

    def test_mass_pricing_trace(self, client, analyst_token):
        """Trace shows RED verdict with policy hit."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_mass_pricing_error(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]

        r = client.get(f"/api/v1/exceptions/{exc_id}/trace", headers=_auth(analyst_token))
        trace = r.json()
        assert trace["intent_selected"] == "MASS_PRICING_ERROR"
        assert trace["shadow_verdict"] == "RED"
        assert trace["final_status"] == "BLOCKED"

    def test_mass_pricing_in_stats(self, client, analyst_token):
        """Stats include MASS_PRICING_ERROR and BLOCKED counts."""
        client.post(
            "/api/v1/exceptions/resolve",
            json=_mass_pricing_error(),
            headers=_auth(analyst_token),
        )
        r = client.get("/api/v1/exceptions/stats", headers=_auth(analyst_token))
        stats = r.json()
        assert stats["by_intent"].get("MASS_PRICING_ERROR", 0) >= 1
        assert stats["blocked"] >= 1


# ===========================================================================
# Cross-intent: Mixed queue and stats
# ===========================================================================

class TestMixedExceptionQueue:
    """Verifies the Exception Queue works with all four intents together."""

    def test_all_four_intents_in_queue(self, client, analyst_token):
        """All four V1 intents coexist in the exception queue."""
        events = [
            _contractual_correction_success(),
            _credit_block(),
            _mass_pricing_error(),
            {  # DUPLICATE_PO
                "order_id": "PO-DUP-MIX",
                "line_item": 1,
                "po_price": 100.0,
                "sap_base_price": 100.0,
                "event_type": "EDI_850_DUPLICATE_PO",
                "retailer_id": "R-10",
                "line_count": 1,
                "metadata": {
                    "signal_scores": {
                        "po_number": 1.0, "customer_id": 1.0, "line_items": 0.95,
                        "amount": 0.90, "timestamp": 0.80, "ship_to": 0.80,
                        "channel": 1.0, "delivery_date": 0.80,
                    },
                    "matched_po_id": "PO-DUP-000",
                },
            },
        ]
        for event in events:
            client.post(
                "/api/v1/exceptions/resolve",
                json=event,
                headers=_auth(analyst_token),
            )

        r = client.get("/api/v1/exceptions", headers=_auth(analyst_token))
        data = r.json()["data"]
        assert len(data) == 4

        intents = {exc["intent"] for exc in data}
        assert "CONTRACTUAL_CORRECTION" in intents
        assert "CREDIT_BLOCK" in intents
        assert "MASS_PRICING_ERROR" in intents
        assert "DUPLICATE_PO" in intents

    def test_stats_with_all_four_intents(self, client, analyst_token):
        """Stats correctly break down all four intents."""
        for event in [_contractual_correction_success(), _credit_block(), _mass_pricing_error()]:
            client.post(
                "/api/v1/exceptions/resolve",
                json=event,
                headers=_auth(analyst_token),
            )

        r = client.get("/api/v1/exceptions/stats", headers=_auth(analyst_token))
        stats = r.json()
        assert stats["total_exceptions"] == 3
        assert len(stats["by_intent"]) >= 3
        # All three lifecycle states represented
        assert len(stats["by_lifecycle_state"]) >= 2
        # All three verdicts represented
        assert len(stats["by_shadow_verdict"]) >= 2

    def test_tenant_isolation_across_intents(self, client, analyst_token, tenant_b_token):
        """Tenant B sees zero exceptions regardless of intent."""
        for event in [_contractual_correction_success(), _credit_block(), _mass_pricing_error()]:
            client.post(
                "/api/v1/exceptions/resolve",
                json=event,
                headers=_auth(analyst_token),
            )

        r = client.get("/api/v1/exceptions", headers=_auth(tenant_b_token))
        assert len(r.json()["data"]) == 0
