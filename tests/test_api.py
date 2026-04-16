"""Tests for the ASOE FastAPI API layer.

Covers: health, resolve, explain, list/detail/trace, override,
approve/reject, stats, workflows, policies, auth endpoints,
JWT auth, RBAC, tenant isolation, and error envelope.
"""

from __future__ import annotations

import os

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
    """Create a fresh FastAPI app per test."""
    return create_app()


@pytest.fixture()
def client(app):
    """TestClient with fresh exception store."""
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
def viewer_token():
    return create_test_token(roles=["viewer"], org="tenant-a")


@pytest.fixture()
def tenant_b_token():
    return create_test_token(roles=["analyst"], org="tenant-b")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sample_event() -> dict:
    return {
        "order_id": "PO-001",
        "po_price": 100.0,
        "sap_base_price": 120.0,
        "event_type": "EDI_850_PRICE_MISMATCH",
    }


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_no_auth(self, client):
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert isinstance(data["kill_switch"], bool)
        assert isinstance(data["explain_mode"], bool)

    def test_health_dynamic_enums(self, client):
        """V1 Foundation Guardrail #2: intents/recipes served dynamically."""
        r = client.get("/api/v1/health")
        data = r.json()
        assert "CONTRACTUAL_CORRECTION" in data["allowed_intents"]
        assert "DUPLICATE_PO" in data["allowed_intents"]
        assert len(data["allowed_recipes"]) >= 3
        assert "INGESTED" in data["lifecycle_states"]
        assert "CLOSED" in data["lifecycle_states"]
        assert len(data["lifecycle_states"]) == 12


# ---------------------------------------------------------------------------
# Auth: JWT validation
# ---------------------------------------------------------------------------

class TestAuth:
    def test_missing_auth_header(self, client):
        r = client.post("/api/v1/exceptions/resolve", json=_sample_event())
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "UNAUTHORIZED"

    def test_invalid_token(self, client):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers={"Authorization": "Bearer invalid.token.here"},
        )
        assert r.status_code == 401

    def test_valid_token(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------

class TestRBAC:
    def test_viewer_cannot_resolve(self, client, viewer_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers=_auth(viewer_token),
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "FORBIDDEN"

    def test_analyst_can_resolve(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200

    def test_analyst_cannot_override(self, client, analyst_token, manager_token):
        # Create a PENDING_REVIEW exception via explain mode
        r = client.post(
            "/api/v1/exceptions/resolve/explain",
            json=_sample_event(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]

        # Analyst cannot override (RBAC: manager+ only)
        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/override",
            json={"action": "ALLOW_BOTH", "notes": "test", "resolved_by": "human"},
            headers=_auth(analyst_token),
        )
        assert r.status_code == 403

    def test_manager_can_override(self, client, analyst_token, manager_token):
        # Create a PENDING_REVIEW exception via explain mode
        r = client.post(
            "/api/v1/exceptions/resolve/explain",
            json=_sample_event(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]

        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/override",
            json={"action": "ALLOW_BOTH", "notes": "Verified with buyer", "resolved_by": "manager-user"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 200

    def test_viewer_can_list(self, client, viewer_token):
        r = client.get("/api/v1/exceptions", headers=_auth(viewer_token))
        assert r.status_code == 200

    def test_viewer_cannot_approve(self, client, viewer_token):
        r = client.post(
            "/api/v1/exceptions/fake-id/approve",
            json={},
            headers=_auth(viewer_token),
        )
        assert r.status_code == 403

    def test_admin_can_update_policy(self, client, admin_token):
        r = client.put(
            "/api/v1/policies/tenant-a",
            json={"policy_key": "global.MAX_DISCOUNT_ALLOWED", "value": 0.20},
            headers=_auth(admin_token),
        )
        assert r.status_code == 200

    def test_analyst_cannot_update_policy(self, client, analyst_token):
        r = client.put(
            "/api/v1/policies/tenant-a",
            json={"policy_key": "global.MAX_DISCOUNT_ALLOWED", "value": 0.20},
            headers=_auth(analyst_token),
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------

class TestTenantIsolation:
    def test_tenant_cannot_see_other_tenant(self, client, analyst_token, tenant_b_token):
        # Create exception in tenant-a
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]

        # Tenant-b cannot see it
        r = client.get(
            f"/api/v1/exceptions/{exc_id}",
            headers=_auth(tenant_b_token),
        )
        assert r.status_code == 404

    def test_tenant_list_is_scoped(self, client, analyst_token, tenant_b_token):
        # Create exception in tenant-a
        client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers=_auth(analyst_token),
        )

        # Tenant-b list is empty
        r = client.get("/api/v1/exceptions", headers=_auth(tenant_b_token))
        assert r.status_code == 200
        assert len(r.json()["data"]) == 0


# ---------------------------------------------------------------------------
# Resolve endpoints
# ---------------------------------------------------------------------------

class TestResolve:
    def test_sync_resolve_returns_result(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert "exception_id" in data
        assert data["intent"] is not None
        assert data["final_status"] is not None

    def test_sync_resolve_credit_block(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve",
            json={
                "order_id": "PO-CB-001",
                "po_price": 100.0,
                "sap_base_price": 100.0,
                "event_type": "CREDIT_BLOCK",
                "requester_role": "ORDER_MANAGER",
                "credit_limit": 50000.0,
                "current_exposure": 48000.0,
            },
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["intent"] == "CREDIT_BLOCK"

    def test_async_resolve(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve/async",
            json=_sample_event(),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert "task_id" in data

    def test_explain_resolve(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve/explain",
            json=_sample_event(),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["final_status"] == "MANUAL_REVIEW_REQUIRED"

    def test_explain_does_not_leak_env(self, client, analyst_token):
        """Explain endpoint restores ASOE_EXPLAIN_MODE after execution."""
        prev = os.environ.get("ASOE_EXPLAIN_MODE")
        client.post(
            "/api/v1/exceptions/resolve/explain",
            json=_sample_event(),
            headers=_auth(analyst_token),
        )
        assert os.environ.get("ASOE_EXPLAIN_MODE") == prev


# ---------------------------------------------------------------------------
# Exception CRUD
# ---------------------------------------------------------------------------

class TestExceptionCRUD:
    def _create_exception(self, client, token) -> str:
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers=_auth(token),
        )
        return r.json()["exception_id"]

    def test_list_exceptions(self, client, analyst_token):
        self._create_exception(client, analyst_token)
        self._create_exception(client, analyst_token)
        r = client.get("/api/v1/exceptions", headers=_auth(analyst_token))
        assert r.status_code == 200
        data = r.json()
        assert len(data["data"]) == 2
        assert isinstance(data["has_more"], bool)

    def test_get_exception_detail(self, client, analyst_token):
        exc_id = self._create_exception(client, analyst_token)
        r = client.get(
            f"/api/v1/exceptions/{exc_id}",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == exc_id
        assert data["order_id"] == "PO-001"
        assert data["tenant_id"] == "tenant-a"

    def test_get_exception_not_found(self, client, analyst_token):
        r = client.get(
            "/api/v1/exceptions/nonexistent",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"

    def test_get_trace(self, client, analyst_token):
        exc_id = self._create_exception(client, analyst_token)
        r = client.get(
            f"/api/v1/exceptions/{exc_id}/trace",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert "trace_id" in data
        assert data["event_id"] == "PO-001"
        assert data["intent_selected"] is not None

    def test_list_filter_by_intent(self, client, analyst_token):
        self._create_exception(client, analyst_token)
        r = client.get(
            "/api/v1/exceptions?intent=CONTRACTUAL_CORRECTION",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200

    def test_stats(self, client, analyst_token):
        self._create_exception(client, analyst_token)
        r = client.get("/api/v1/exceptions/stats", headers=_auth(analyst_token))
        assert r.status_code == 200
        data = r.json()
        assert data["total_exceptions"] == 1
        assert "open_exceptions" in data
        assert "auto_resolved" in data
        assert "manual_review" in data
        assert "by_intent" in data
        assert isinstance(data["by_intent"], dict)
        assert "by_lifecycle_state" in data
        assert isinstance(data["by_lifecycle_state"], dict)
        assert "by_shadow_verdict" in data
        assert isinstance(data["by_shadow_verdict"], dict)
        # avg_resolution_time_seconds may be None or a float
        assert "avg_resolution_time_seconds" in data


# ---------------------------------------------------------------------------
# Override
# ---------------------------------------------------------------------------

class TestOverride:
    def test_override_success(self, client, analyst_token, manager_token):
        # Override requires PENDING_REVIEW state — use explain mode
        r = client.post(
            "/api/v1/exceptions/resolve/explain",
            json=_sample_event(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]

        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/override",
            json={
                "action": "ALLOW_BOTH",
                "notes": "Verified with buyer",
                "resolved_by": "manager@example.com",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["resolved_by"] == "manager@example.com"
        assert data["resolved_action"] == "ALLOW_BOTH"
        assert data["lifecycle_state"] == "RESOLVED"


# ---------------------------------------------------------------------------
# Approve / Reject
# ---------------------------------------------------------------------------

class TestApproveReject:
    def _create_pending_review(self, client, token) -> str:
        """Create an exception in PENDING_REVIEW state via explain mode."""
        r = client.post(
            "/api/v1/exceptions/resolve/explain",
            json=_sample_event(),
            headers=_auth(token),
        )
        exc_id = r.json()["exception_id"]
        return exc_id

    def test_approve_pending_review(self, client, analyst_token, manager_token):
        exc_id = self._create_pending_review(client, analyst_token)
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/approve",
            json={"notes": "Approved after review"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        assert r.json()["lifecycle_state"] == "EXECUTING"

    def test_reject_pending_review(self, client, analyst_token, manager_token):
        exc_id = self._create_pending_review(client, analyst_token)
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/reject",
            json={"reason": "Not valid"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        assert r.json()["lifecycle_state"] == "REJECTED"

    def test_approve_wrong_state(self, client, analyst_token, manager_token):
        # Create a RESOLVED exception (not PENDING_REVIEW)
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]

        r = client.post(
            f"/api/v1/exceptions/{exc_id}/approve",
            json={},
            headers=_auth(manager_token),
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "INVALID_STATE"


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------

class TestWorkflows:
    def test_workflow_execution(self, client, manager_token):
        r = client.post(
            "/api/v1/workflows",
            json={
                "workflow_id": "wf-001",
                "name": "Test Workflow",
                "steps": [
                    {
                        "step_id": "s1",
                        "intent": "CONTRACTUAL_CORRECTION",
                        "description": "Price correction",
                    }
                ],
                "base_event": _sample_event(),
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["workflow_id"] == "wf-001"
        assert "step_results" in data

    def test_workflow_invalid_intent(self, client, manager_token):
        r = client.post(
            "/api/v1/workflows",
            json={
                "workflow_id": "wf-002",
                "name": "Bad Workflow",
                "steps": [
                    {
                        "step_id": "s1",
                        "intent": "NONEXISTENT_INTENT",
                        "description": "Will fail",
                    }
                ],
                "base_event": _sample_event(),
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_INTENT"


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

class TestPolicies:
    def test_update_policy(self, client, admin_token):
        r = client.put(
            "/api/v1/policies/tenant-a",
            json={
                "policy_key": "global.MAX_DISCOUNT_ALLOWED",
                "value": 0.25,
                "change_reason": "Seasonal adjustment",
            },
            headers=_auth(admin_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["policy_key"] == "global.MAX_DISCOUNT_ALLOWED"
        assert data["value"] == 0.25
        assert data["tenant_id"] == "tenant-a"
        assert data["created_by"] == "test-user"


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

class TestAuthEndpoints:
    def test_login_known_user(self, client):
        """Login with a known user email returns tokens + profile."""
        r = client.post(
            "/api/auth/login",
            json={"email": "marcus.webb@acme-corp.com", "password": "password"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["access_token"] != ""
        assert data["user"]["name"] == "Marcus Webb"
        assert data["user"]["title"] == "Admin"
        assert "admin" in data["user"]["roles"]
        assert "visible_tabs" in data["user"]
        assert "settings" in data["user"]["visible_tabs"]

    def test_login_unknown_user(self, client):
        """Login with unknown email returns 401."""
        r = client.post(
            "/api/auth/login",
            json={"email": "unknown@example.com", "password": "password"},
        )
        assert r.status_code == 401

    def test_login_analyst_has_limited_tabs(self, client):
        """Analyst user has no settings tab."""
        r = client.post(
            "/api/auth/login",
            json={"email": "james.ortiz@acme-corp.com", "password": "password"},
        )
        assert r.status_code == 200
        data = r.json()
        assert "settings" not in data["user"]["visible_tabs"]
        assert "exceptions" in data["user"]["visible_tabs"]
        assert data["user"]["assigned_accounts"] == ["acct-walmart", "acct-kroger"]

    def test_mfa_verify(self, client):
        r = client.post(
            "/api/auth/mfa/verify",
            json={"mfa_token": "any-token", "code": "123456"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["access_token"] != ""
        assert data["user"] is not None

    def test_sso_init(self, client):
        r = client.post("/api/auth/sso/init")
        assert r.status_code == 200
        assert "redirect_url" in r.json()

    def test_sso_callback(self, client):
        r = client.get("/api/auth/sso/callback")
        assert r.status_code == 200
        assert "access_token" in r.json()

    def test_refresh_token(self, client):
        from api.deps import create_refresh_token
        refresh = create_refresh_token(
            sub="test-user", email="test@example.com", name="Test",
            roles=["analyst"], org="tenant-a",
        )
        r = client.post(
            "/api/auth/refresh",
            json={"refresh_token": refresh},
        )
        assert r.status_code == 200
        assert r.json()["access_token"] != ""

    def test_refresh_invalid_token(self, client):
        r = client.post(
            "/api/auth/refresh",
            json={"refresh_token": "invalid.token.here"},
        )
        assert r.status_code == 401

    def test_me(self, client, analyst_token):
        r = client.get("/api/auth/me", headers=_auth(analyst_token))
        assert r.status_code == 200
        data = r.json()
        assert data["email"] == "test@example.com"
        assert "analyst" in data["roles"]
        assert data["org"] == "tenant-a"
        assert "visible_tabs" in data
        assert isinstance(data["visible_tabs"], list)

    def test_switch_user_sandbox(self, client):
        """Switch user in sandbox mode returns new JWT for target user."""
        # Login as admin first
        r = client.post(
            "/api/auth/login",
            json={"email": "marcus.webb@acme-corp.com", "password": "password"},
        )
        admin_token = r.json()["access_token"]

        # Switch to James Ortiz
        r = client.post(
            "/api/auth/switch",
            json={"email": "james.ortiz@acme-corp.com", "password": ""},
            headers=_auth(admin_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["user"]["name"] == "James Ortiz"
        assert data["user"]["assigned_accounts"] == ["acct-walmart", "acct-kroger"]
        assert "settings" not in data["user"]["visible_tabs"]

    def test_switch_user_unknown(self, client):
        """Switch to unknown user returns 404."""
        r = client.post(
            "/api/auth/login",
            json={"email": "marcus.webb@acme-corp.com", "password": "password"},
        )
        admin_token = r.json()["access_token"]

        r = client.post(
            "/api/auth/switch",
            json={"email": "nobody@example.com", "password": ""},
            headers=_auth(admin_token),
        )
        assert r.status_code == 404

    def test_list_users_sandbox(self, client):
        """List users returns all 5 seed users in sandbox mode."""
        r = client.post(
            "/api/auth/login",
            json={"email": "marcus.webb@acme-corp.com", "password": "password"},
        )
        token = r.json()["access_token"]

        r = client.get("/api/auth/users", headers=_auth(token))
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data) == 5
        names = [u["name"] for u in data]
        assert "Marcus Webb" in names
        assert "James Ortiz" in names


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------

class TestErrorEnvelope:
    def test_error_format(self, client):
        r = client.post("/api/v1/exceptions/resolve", json=_sample_event())
        assert r.status_code == 401
        data = r.json()
        assert "error" in data
        assert "code" in data["error"]
        assert "message" in data["error"]

    def test_not_found_error(self, client, analyst_token):
        r = client.get(
            "/api/v1/exceptions/no-such-id",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Line Items endpoint
# ---------------------------------------------------------------------------

class TestLineItems:
    def _create_exception(self, client, token) -> str:
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers=_auth(token),
        )
        return r.json()["exception_id"]

    def test_line_items_happy_path(self, client, analyst_token):
        exc_id = self._create_exception(client, analyst_token)
        r = client.get(
            f"/api/v1/exceptions/{exc_id}/line-items",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert "data" in data
        assert isinstance(data["data"], list)

    def test_line_items_not_found(self, client, analyst_token):
        r = client.get(
            "/api/v1/exceptions/nonexistent/line-items",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"

    def test_line_items_unauthenticated(self, client):
        r = client.get("/api/v1/exceptions/some-id/line-items")
        assert r.status_code == 401

    def test_line_items_viewer_forbidden(self, client, viewer_token):
        r = client.get(
            "/api/v1/exceptions/some-id/line-items",
            headers=_auth(viewer_token),
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Analysis endpoint
# ---------------------------------------------------------------------------

class TestAnalysis:
    def _create_exception(self, client, token) -> str:
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers=_auth(token),
        )
        return r.json()["exception_id"]

    def test_analysis_happy_path(self, client, analyst_token):
        exc_id = self._create_exception(client, analyst_token)
        r = client.get(
            f"/api/v1/exceptions/{exc_id}/analysis",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert "diagnosis" in data
        assert "confidence" in data
        assert "risk" in data
        assert "resolution" in data
        assert "lines" in data
        assert isinstance(data["lines"], list)

    def test_analysis_not_found(self, client, analyst_token):
        r = client.get(
            "/api/v1/exceptions/nonexistent/analysis",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"

    def test_analysis_unauthenticated(self, client):
        r = client.get("/api/v1/exceptions/some-id/analysis")
        assert r.status_code == 401

    def test_analysis_viewer_forbidden(self, client, viewer_token):
        r = client.get(
            "/api/v1/exceptions/some-id/analysis",
            headers=_auth(viewer_token),
        )
        assert r.status_code == 403
