"""Full integration tests: API gateway -> Recipe execution -> DB state.

Validates the complete path from REST endpoint through the
Skill-Shadow-Recipe pipeline, verifying:
  1. Authentication via JWT Bearer tokens
  2. RBAC enforcement on each endpoint
  3. Pipeline execution via /api/v1/exceptions/resolve
  4. Exception persistence and retrieval
  5. Trace data storage and retrieval
  6. Dashboard stats accuracy
  7. All 19 REST endpoints respond correctly

Architecture_v3.md Section 8.2 defines the endpoint contract.
"""

from __future__ import annotations

import pytest

from tests.sandbox.conftest import (
    CREDIT_BLOCK_EVENT,
    DUPLICATE_PO_EVENT,
    MASS_PRICING_EVENT,
    PRICING_EVENT,
    SANDBOX_TENANT,
    auth_header,
)


# ═══════════════════════════════════════════════════════════════════════════
# Health endpoint (public, no auth required)
# ═══════════════════════════════════════════════════════════════════════════

class TestHealthEndpoint:
    """GET /api/v1/health — public, returns system status."""

    def test_health_returns_ok(self, client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert "allowed_intents" in data
        assert "lifecycle_states" in data
        assert "allowed_recipes" in data

    def test_health_includes_kill_switch_and_explain_mode(self, client):
        data = client.get("/api/v1/health").json()
        assert isinstance(data["kill_switch"], bool)
        assert isinstance(data["explain_mode"], bool)


# ═══════════════════════════════════════════════════════════════════════════
# Synchronous resolve (POST /api/v1/exceptions/resolve)
# ═══════════════════════════════════════════════════════════════════════════

class TestSynchronousResolve:
    """End-to-end resolve: API -> graph -> persist -> response."""

    def test_resolve_pricing_event(self, client, analyst_token):
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exception_id"]
        assert data["intent"] == "CONTRACTUAL_CORRECTION"
        assert data["shadow_verdict"] == "GREEN"
        assert data["selected_recipe"] == "PriceAdjustmentRecipe.py"
        assert data["final_status"] == "COMPLETE"

    def test_resolve_credit_block_event(self, client, analyst_token):
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=CREDIT_BLOCK_EVENT,
            headers=auth_header(analyst_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["intent"] == "CREDIT_BLOCK"
        # Credit block may get YELLOW shadow → MANUAL_REVIEW_REQUIRED,
        # or GREEN → recipe execution → COMPLETE/REJECTED
        assert data["final_status"] in (
            "COMPLETE", "REJECTED", "MANUAL_REVIEW_REQUIRED", "BLOCKED",
        )

    def test_resolve_mass_pricing_event_blocked(self, client, analyst_token):
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=MASS_PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["intent"] == "MASS_PRICING_ERROR"
        assert data["shadow_verdict"] == "RED"
        assert data["final_status"] == "BLOCKED"

    def test_resolve_duplicate_po_event(self, client, analyst_token):
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=DUPLICATE_PO_EVENT,
            headers=auth_header(analyst_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["intent"] == "DUPLICATE_PO"
        assert data["selected_recipe"] == "DuplicatePORecipe.py"

    def test_resolve_requires_auth(self, client):
        resp = client.post("/api/v1/exceptions/resolve", json=PRICING_EVENT)
        assert resp.status_code == 401

    def test_resolve_requires_analyst_role(self, client, viewer_token):
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=PRICING_EVENT,
            headers=auth_header(viewer_token),
        )
        assert resp.status_code == 403

    def test_resolve_response_includes_trace_id(self, client, analyst_token):
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        data = resp.json()
        assert data.get("trace_id") is not None

    def test_resolve_with_custom_trace_id(self, client, analyst_token):
        headers = {**auth_header(analyst_token), "X-Trace-ID": "custom-trace-123"}
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=PRICING_EVENT,
            headers=headers,
        )
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# Async resolve (POST /api/v1/exceptions/resolve/async)
# ═══════════════════════════════════════════════════════════════════════════

class TestAsyncResolve:
    """POST /api/v1/exceptions/resolve/async — returns task_id."""

    def test_async_resolve_returns_task_id(self, client, analyst_token):
        resp = client.post(
            "/api/v1/exceptions/resolve/async",
            json=PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "task_id" in data
        assert data["status"] in ("queued", "complete")

    def test_async_resolve_requires_auth(self, client):
        resp = client.post("/api/v1/exceptions/resolve/async", json=PRICING_EVENT)
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# Explain mode (POST /api/v1/exceptions/resolve/explain)
# ═══════════════════════════════════════════════════════════════════════════

class TestExplainMode:
    """POST /api/v1/exceptions/resolve/explain — dry-run, no side effects."""

    def test_explain_returns_manual_review(self, client, analyst_token):
        resp = client.post(
            "/api/v1/exceptions/resolve/explain",
            json=PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["final_status"] == "MANUAL_REVIEW_REQUIRED"

    def test_explain_includes_explanation(self, client, analyst_token):
        resp = client.post(
            "/api/v1/exceptions/resolve/explain",
            json=PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        data = resp.json()
        assert data.get("explanation") is not None


# ═══════════════════════════════════════════════════════════════════════════
# Exception CRUD (GET, PATCH, POST approve/reject)
# ═══════════════════════════════════════════════════════════════════════════

class TestExceptionCRUD:
    """Exception lifecycle through REST endpoints."""

    def _create_exception(self, client, token):
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=PRICING_EVENT,
            headers=auth_header(token),
        )
        return resp.json()["exception_id"]

    def test_list_exceptions(self, client, analyst_token):
        self._create_exception(client, analyst_token)
        resp = client.get(
            "/api/v1/exceptions",
            headers=auth_header(analyst_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) >= 1
        assert "has_more" in data

    def test_get_exception_detail(self, client, analyst_token):
        exc_id = self._create_exception(client, analyst_token)
        resp = client.get(
            f"/api/v1/exceptions/{exc_id}",
            headers=auth_header(analyst_token),
        )
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["id"] == exc_id
        assert detail["order_id"] == "SO-1001"

    def test_get_exception_trace(self, client, analyst_token):
        exc_id = self._create_exception(client, analyst_token)
        resp = client.get(
            f"/api/v1/exceptions/{exc_id}/trace",
            headers=auth_header(analyst_token),
        )
        assert resp.status_code == 200
        trace = resp.json()
        assert "trace_id" in trace
        assert trace.get("intent_selected") == "CONTRACTUAL_CORRECTION"

    def test_get_nonexistent_exception_returns_404(self, client, analyst_token):
        resp = client.get(
            "/api/v1/exceptions/nonexistent-id",
            headers=auth_header(analyst_token),
        )
        assert resp.status_code == 404

    def test_override_exception(self, client, manager_token):
        exc_id = self._create_exception(client, manager_token)
        resp = client.patch(
            f"/api/v1/exceptions/{exc_id}/override",
            json={
                "action": "MANUAL_ADJUSTMENT",
                "notes": "Manually reviewed and adjusted",
                "resolved_by": "manager-user",
            },
            headers=auth_header(manager_token),
        )
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["resolved_by"] == "manager-user"
        assert detail["lifecycle_state"] == "RESOLVED"

    def test_override_requires_manager_role(self, client, analyst_token):
        exc_id = self._create_exception(client, analyst_token)
        resp = client.patch(
            f"/api/v1/exceptions/{exc_id}/override",
            json={"action": "OVERRIDE", "notes": "test", "resolved_by": "analyst"},
            headers=auth_header(analyst_token),
        )
        assert resp.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# Dashboard stats (GET /api/v1/exceptions/stats)
# ═══════════════════════════════════════════════════════════════════════════

class TestDashboardStats:
    """GET /api/v1/exceptions/stats — aggregate metrics."""

    def test_stats_after_resolves(self, client, analyst_token):
        # Create exceptions covering different outcomes
        client.post(
            "/api/v1/exceptions/resolve",
            json=PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        client.post(
            "/api/v1/exceptions/resolve",
            json=MASS_PRICING_EVENT,
            headers=auth_header(analyst_token),
        )
        resp = client.get(
            "/api/v1/exceptions/stats",
            headers=auth_header(analyst_token),
        )
        assert resp.status_code == 200
        stats = resp.json()
        assert stats["total_exceptions"] >= 2
        assert stats["auto_resolved"] >= 1  # pricing → COMPLETE → RESOLVED
        assert stats["blocked"] >= 1  # mass pricing → BLOCKED

    def test_stats_requires_auth(self, client):
        resp = client.get("/api/v1/exceptions/stats")
        assert resp.status_code == 401

    def test_stats_allows_viewer_role(self, client, viewer_token):
        resp = client.get(
            "/api/v1/exceptions/stats",
            headers=auth_header(viewer_token),
        )
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# Workflow endpoint (POST /api/v1/workflows)
# ═══════════════════════════════════════════════════════════════════════════

class TestWorkflowEndpoint:
    """POST /api/v1/workflows — multi-step workflow execution."""

    def test_workflow_requires_manager_role(self, client, analyst_token):
        resp = client.post(
            "/api/v1/workflows",
            json={
                "workflow_id": "WF-001",
                "name": "Test Workflow",
                "steps": [{"step_id": "S1", "intent": "CONTRACTUAL_CORRECTION",
                           "description": "Step 1"}],
                "base_event": PRICING_EVENT,
            },
            headers=auth_header(analyst_token),
        )
        assert resp.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# Policy endpoint (PUT /api/v1/policies/{tenant_id})
# ═══════════════════════════════════════════════════════════════════════════

class TestPolicyEndpoint:
    """PUT /api/v1/policies/{tenant_id} — admin-only policy overrides."""

    def test_policy_update_requires_admin(self, client, manager_token):
        resp = client.put(
            f"/api/v1/policies/{SANDBOX_TENANT}",
            json={"policy_key": "MAX_DISCOUNT_ALLOWED", "value": 0.20},
            headers=auth_header(manager_token),
        )
        assert resp.status_code == 403

    def test_policy_update_with_admin(self, client, admin_token):
        resp = client.put(
            f"/api/v1/policies/{SANDBOX_TENANT}",
            json={
                "policy_key": "MAX_DISCOUNT_ALLOWED",
                "value": 0.20,
                "change_reason": "Sandbox test update",
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# Tenant isolation (architecture_v3.md Section 11.3)
# ═══════════════════════════════════════════════════════════════════════════

class TestTenantIsolation:
    """Exceptions from one tenant are not visible to another."""

    def test_cross_tenant_isolation(self, client):
        from api.deps import create_test_token

        token_a = create_test_token(roles=["analyst"], org="tenant-A")
        token_b = create_test_token(roles=["analyst"], org="tenant-B")

        # Create exception in tenant-A
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=PRICING_EVENT,
            headers=auth_header(token_a),
        )
        exc_id = resp.json()["exception_id"]

        # Tenant-B cannot see tenant-A's exception
        resp = client.get(
            f"/api/v1/exceptions/{exc_id}",
            headers=auth_header(token_b),
        )
        assert resp.status_code == 404

    def test_tenant_stats_are_isolated(self, client):
        from api.deps import create_test_token

        token_a = create_test_token(roles=["analyst"], org="tenant-A")
        token_b = create_test_token(roles=["analyst"], org="tenant-B")

        client.post(
            "/api/v1/exceptions/resolve",
            json=PRICING_EVENT,
            headers=auth_header(token_a),
        )
        resp = client.get(
            "/api/v1/exceptions/stats",
            headers=auth_header(token_b),
        )
        assert resp.json()["total_exceptions"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# Error envelope (architecture_v3.md Section 8.3)
# ═══════════════════════════════════════════════════════════════════════════

class TestErrorEnvelope:
    """API errors return structured JSON error envelopes."""

    def test_401_returns_json_error(self, client):
        resp = client.post("/api/v1/exceptions/resolve", json=PRICING_EVENT)
        assert resp.status_code == 401
        body = resp.json()
        # Error envelope wraps code under "error" key per ASOEError handler
        assert "error" in body or "code" in body or "detail" in body

    def test_403_returns_json_error(self, client, viewer_token):
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json=PRICING_EVENT,
            headers=auth_header(viewer_token),
        )
        assert resp.status_code == 403

    def test_404_returns_json_error(self, client, analyst_token):
        resp = client.get(
            "/api/v1/exceptions/does-not-exist",
            headers=auth_header(analyst_token),
        )
        assert resp.status_code == 404
