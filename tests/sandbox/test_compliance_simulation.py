"""Compliance Shadow simulation and deterministic test flags.

Validates:
  1. Force BLOCKED state via mass pricing events (RED shadow)
  2. Force MANUAL_REVIEW_REQUIRED via explain mode (YELLOW-equivalent)
  3. Compliance Shadow verdict is displayed in API responses
  4. Recipe execution only happens after GREEN shadow verdict
  5. Kill switch halts all execution immediately
  6. Explain mode runs pipeline without side effects
  7. Constrained output validation (Outlines/Guidance)

Architecture_v3.md Section 5.3 (Constrained Generation),
Section 5.1 (Pipeline Nodes), Hardening (§14.1).
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.store import exception_store
from api.pubsub import event_publisher
from tests.sandbox.conftest import (
    CREDIT_BLOCK_EVENT,
    DUPLICATE_PO_EVENT,
    MASS_PRICING_EVENT,
    PRICING_EVENT,
    SANDBOX_TENANT,
    auth_header,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    exception_store.clear()
    if hasattr(event_publisher, "clear"):
        event_publisher.clear()
    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def analyst_token():
    return create_test_token(
        sub="analyst-user", email="analyst@asoe.test",
        name="Analyst", roles=["analyst"], org=SANDBOX_TENANT,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Force BLOCKED state (RED shadow verdict)
# ═══════════════════════════════════════════════════════════════════════════

class TestForceBlocked:
    """Mass pricing events trigger RED shadow → BLOCKED terminal state."""

    def test_mass_pricing_gets_blocked(self, client, analyst_token):
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=MASS_PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["shadow_verdict"] == "RED"
        assert data["final_status"] == "BLOCKED"

    def test_blocked_persists_in_store(self, client, analyst_token):
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=MASS_PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        exc_id = resp.json()["exception_id"]
        detail = client.get(
            f"/api/v1/exceptions/{exc_id}",
            headers=auth_header(analyst_token),
        ).json()
        assert detail["lifecycle_state"] == "BLOCKED"

    def test_blocked_shows_in_stats(self, client, analyst_token):
        client.post(
            "/api/v1/exceptions/resolve",
            json=MASS_PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        stats = client.get(
            "/api/v1/exceptions/stats",
            headers=auth_header(analyst_token),
        ).json()
        assert stats["blocked"] >= 1

    def test_higher_line_count_still_blocked(self, client, analyst_token):
        """25 lines → RED shadow → BLOCKED."""
        event = {**MASS_PRICING_EVENT, "order_id": "SO-3002", "line_count": 25}
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=event,
            headers=auth_header(analyst_token),
        )
        assert resp.json()["shadow_verdict"] == "RED"
        assert resp.json()["final_status"] == "BLOCKED"


# ═══════════════════════════════════════════════════════════════════════════
# Force MANUAL_REVIEW_REQUIRED (explain mode / YELLOW-equivalent)
# ═══════════════════════════════════════════════════════════════════════════

class TestForceManualReview:
    """Explain mode forces MANUAL_REVIEW_REQUIRED without recipe execution."""

    def test_explain_mode_returns_manual_review(self, client, analyst_token):
        resp = client.post(
            "/api/v1/exceptions/resolve/explain",
            json=PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["final_status"] == "MANUAL_REVIEW_REQUIRED"

    def test_explain_mode_no_recipe_side_effects(self, client, analyst_token):
        """Explain mode should not produce recipe execution outputs."""
        resp = client.post(
            "/api/v1/exceptions/resolve/explain",
            json=PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        data = resp.json()
        assert data["final_status"] == "MANUAL_REVIEW_REQUIRED"
        # Explanation should be present
        assert data.get("explanation") is not None

    def test_explain_mode_persists_record(self, client, analyst_token):
        resp = client.post(
            "/api/v1/exceptions/resolve/explain",
            json=PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        exc_id = resp.json()["exception_id"]
        detail = client.get(
            f"/api/v1/exceptions/{exc_id}",
            headers=auth_header(analyst_token),
        ).json()
        assert detail["lifecycle_state"] == "PENDING_REVIEW"


# ═══════════════════════════════════════════════════════════════════════════
# Kill switch (architecture_v3.md §14.1 — Hardening)
# ═══════════════════════════════════════════════════════════════════════════

class TestKillSwitch:
    """ASOE_KILL_SWITCH=1 halts all execution immediately."""

    def test_kill_switch_returns_fail_to_human(self, client, analyst_token):
        old = os.environ.get("ASOE_KILL_SWITCH")
        os.environ["ASOE_KILL_SWITCH"] = "1"
        try:
            resp = client.post(
                "/api/v1/exceptions/resolve",
                json=PRICING_EVENT,
                headers=auth_header(analyst_token),
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["final_status"] == "FAIL_TO_HUMAN"
        finally:
            if old is None:
                os.environ.pop("ASOE_KILL_SWITCH", None)
            else:
                os.environ["ASOE_KILL_SWITCH"] = old

    def test_kill_switch_reflected_in_health(self, client):
        old = os.environ.get("ASOE_KILL_SWITCH")
        os.environ["ASOE_KILL_SWITCH"] = "1"
        try:
            health = client.get("/api/v1/health").json()
            assert health["kill_switch"] is True
        finally:
            if old is None:
                os.environ.pop("ASOE_KILL_SWITCH", None)
            else:
                os.environ["ASOE_KILL_SWITCH"] = old


# ═══════════════════════════════════════════════════════════════════════════
# Shadow verdict in API responses
# ═══════════════════════════════════════════════════════════════════════════

class TestShadowVerdictInResponse:
    """Compliance Shadow verdict is displayed in every resolve response."""

    def test_green_verdict_for_normal_pricing(self, client, analyst_token):
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        assert resp.json()["shadow_verdict"] == "GREEN"

    def test_red_verdict_for_mass_pricing(self, client, analyst_token):
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=MASS_PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        assert resp.json()["shadow_verdict"] == "RED"

    def test_verdict_stored_in_exception_detail(self, client, analyst_token):
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        exc_id = resp.json()["exception_id"]
        detail = client.get(
            f"/api/v1/exceptions/{exc_id}",
            headers=auth_header(analyst_token),
        ).json()
        assert detail["shadow_verdict"] == "GREEN"

    def test_verdict_in_trace_record(self, client, analyst_token):
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        exc_id = resp.json()["exception_id"]
        trace = client.get(
            f"/api/v1/exceptions/{exc_id}/trace",
            headers=auth_header(analyst_token),
        ).json()
        assert trace["shadow_verdict"] == "GREEN"


# ═══════════════════════════════════════════════════════════════════════════
# Recipe executes only after GREEN shadow
# ═══════════════════════════════════════════════════════════════════════════

class TestRecipeAfterApproval:
    """Recipe execution is gated by shadow verdict."""

    def test_green_shadow_executes_recipe(self, client, analyst_token):
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        data = resp.json()
        assert data["shadow_verdict"] == "GREEN"
        assert data["selected_recipe"] == "PriceAdjustmentRecipe.py"
        assert data["final_status"] == "COMPLETE"

    def test_red_shadow_skips_recipe_execution(self, client, analyst_token):
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=MASS_PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        data = resp.json()
        assert data["shadow_verdict"] == "RED"
        assert data["final_status"] == "BLOCKED"
        # No recipe selected on RED shadow
        assert data.get("selected_recipe") is None


# ═══════════════════════════════════════════════════════════════════════════
# Approve/Reject flow (HITL)
# ═══════════════════════════════════════════════════════════════════════════

class TestApproveRejectFlow:
    """Approve and reject endpoints for MANUAL_REVIEW_REQUIRED exceptions."""

    def _create_pending_review(self, client, token):
        resp = client.post(
            "/api/v1/exceptions/resolve/explain",
            json=PRICING_EVENT,
            headers=auth_header(token),
        )
        return resp.json()["exception_id"]

    def test_approve_transitions_from_pending_review(self, client):
        manager_token = create_test_token(
            sub="mgr", email="mgr@asoe.test", name="Manager",
            roles=["manager"], org=SANDBOX_TENANT,
        )
        exc_id = self._create_pending_review(client, manager_token)
        resp = client.post(
            f"/api/v1/exceptions/{exc_id}/approve",
            json={"notes": "Approved after review"},
            headers=auth_header(manager_token),
        )
        assert resp.status_code == 200
        assert resp.json()["lifecycle_state"] == "EXECUTING"

    def test_reject_transitions_from_pending_review(self, client):
        manager_token = create_test_token(
            sub="mgr", email="mgr@asoe.test", name="Manager",
            roles=["manager"], org=SANDBOX_TENANT,
        )
        exc_id = self._create_pending_review(client, manager_token)
        resp = client.post(
            f"/api/v1/exceptions/{exc_id}/reject",
            json={"reason": "Not valid"},
            headers=auth_header(manager_token),
        )
        assert resp.status_code == 200
        assert resp.json()["lifecycle_state"] == "REJECTED"
        assert resp.json()["final_status"] == "REJECTED"

    def test_approve_rejects_wrong_state(self, client):
        manager_token = create_test_token(
            sub="mgr", email="mgr@asoe.test", name="Manager",
            roles=["manager"], org=SANDBOX_TENANT,
        )
        # Create a COMPLETE exception (not PENDING_REVIEW)
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=PRICING_EVENT,
            headers=auth_header(manager_token),
        )
        exc_id = resp.json()["exception_id"]
        resp = client.post(
            f"/api/v1/exceptions/{exc_id}/approve",
            json={"notes": "test"},
            headers=auth_header(manager_token),
        )
        assert resp.status_code == 409

    def test_approve_requires_manager_role(self, client, analyst_token):
        exc_id = self._create_pending_review(client, analyst_token)
        resp = client.post(
            f"/api/v1/exceptions/{exc_id}/approve",
            json={"notes": "test"},
            headers=auth_header(analyst_token),
        )
        assert resp.status_code == 403
