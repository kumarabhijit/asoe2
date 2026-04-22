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
        assert "PENDING_COSIGN" in data["lifecycle_states"]
        # Phase 3: 12 states after removing EXECUTING (legacy /approve
        # transition; disposition goes PENDING_REVIEW → RESOLVED directly).
        assert "EXECUTING" not in data["lifecycle_states"]
        assert len(data["lifecycle_states"]) == 12
        # Resolution actions must be served dynamically so the UI Override
        # chooser never hardcodes codes (Guardrail #2).
        assert isinstance(data["allowed_resolution_actions"], list)
        assert "ALLOW_BOTH" in data["allowed_resolution_actions"]
        assert "ESCALATE" in data["allowed_resolution_actions"]
        # Per-intent reason-tag map (Phase 3 Option A framework). Every
        # intent in allowed_intents must also appear as a key in the map.
        # Today all values equal the global set; curation happens later.
        per_intent = data["allowed_override_reason_tags_by_intent"]
        assert isinstance(per_intent, dict)
        for intent in data["allowed_intents"]:
            assert intent in per_intent, f"missing intent in reason-tag map: {intent}"
            assert "other" in per_intent[intent], (
                f"'other' must be a fallback tag for every intent; missing for {intent}"
            )


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
            f"/api/v1/exceptions/{exc_id}/disposition",
            json={"action": "ALLOW_BOTH", "notes": "test", "reason_tag": "other"},
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
            f"/api/v1/exceptions/{exc_id}/disposition",
            json={"action": "ALLOW_BOTH", "notes": "Verified with buyer", "reason_tag": "other"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 200

    def test_viewer_can_list(self, client, viewer_token):
        r = client.get("/api/v1/exceptions", headers=_auth(viewer_token))
        assert r.status_code == 200

    def test_viewer_cannot_approve(self, client, viewer_token):
        r = client.patch("/api/v1/exceptions/fake-id/disposition",
            json={"action": "ALLOW_BOTH", "reason_tag": "other", },
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
            f"/api/v1/exceptions/{exc_id}/disposition",
            json={
                "action": "ALLOW_BOTH",
                "notes": "Verified with buyer",
                "reason_tag": "other",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        data = r.json()
        # resolved_by is derived from the JWT sub (create_test_token default)
        assert data["resolved_by"] == "test-user"
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
        r = client.patch(f"/api/v1/exceptions/{exc_id}/disposition",
            json={"action": "ALLOW_BOTH", "reason_tag": "other", "notes": "Approved after review"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        assert r.json()["lifecycle_state"] == "RESOLVED"

    def test_reject_pending_review(self, client, analyst_token, manager_token):
        exc_id = self._create_pending_review(client, analyst_token)
        r = client.patch(f"/api/v1/exceptions/{exc_id}/disposition",
            json={"action": "NO_ACTION", "reason_tag": "other", "notes": "Not valid"},
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

        r = client.patch(f"/api/v1/exceptions/{exc_id}/disposition",
            json={"action": "ALLOW_BOTH", "reason_tag": "other", "notes": "should fail"},
            headers=_auth(manager_token),
        )
        # RESOLVED lifecycle + action=ALLOW_BOTH routes to OVERRIDE sub-type;
        # if that record's recommended_action already matches ALLOW_BOTH the
        # path becomes APPROVE, which then fails HITL_DISPOSITION_STATES
        # (RESOLVED is not approvable). Either way, 409 is the shape.
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "INVALID_STATE"


# ---------------------------------------------------------------------------
# Reanalyze
# ---------------------------------------------------------------------------

class TestReanalyze:
    """POST /api/v1/exceptions/{id}/reanalyze — governance-compliant replay."""

    def _create_pending_review(self, client, token) -> str:
        """Create an exception in PENDING_REVIEW (YELLOW) via explain mode."""
        r = client.post(
            "/api/v1/exceptions/resolve/explain",
            json=_sample_event(),
            headers=_auth(token),
        )
        return r.json()["exception_id"]

    def test_reanalyze_success_on_pending_review(self, client, analyst_token, manager_token):
        exc_id = self._create_pending_review(client, analyst_token)
        # Capture prior verdict so we can assert the history entry preserves
        # it verbatim, without coupling the test to which verdict the sample
        # event produces.
        detail_before = client.get(
            f"/api/v1/exceptions/{exc_id}", headers=_auth(manager_token),
        ).json()
        prior_verdict = detail_before["shadow_verdict"]
        prior_trace_id = detail_before["trace_id"]

        r = client.post(
            f"/api/v1/exceptions/{exc_id}/reanalyze",
            json={"reason": "Contract reference updated upstream"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert len(data["reanalysis_history"]) == 1
        entry = data["reanalysis_history"][0]
        assert entry["attempt"] == 1
        assert entry["reason"] == "Contract reference updated upstream"
        # Append-only audit — prior verdict/trace must be preserved exactly.
        assert entry["prior_shadow_verdict"] == prior_verdict
        assert entry["prior_trace_id"] == prior_trace_id
        # New trace must be distinct (fresh run through the graph).
        assert entry["new_trace_id"] != prior_trace_id

    def test_reanalyze_rejected_for_analyst_role(self, client, analyst_token):
        exc_id = self._create_pending_review(client, analyst_token)
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/reanalyze",
            json={"reason": "trying"},
            headers=_auth(analyst_token),
        )
        assert r.status_code == 403

    def test_reanalyze_blocked_on_resolved_green(self, client, analyst_token, manager_token):
        """Ineligibility gate rejects auto-resolved GREEN exceptions — the
        hard stop against re-running a successfully resolved exception in
        search of a different outcome."""
        exc_id = self._create_pending_review(client, analyst_token)
        # Force the record into a terminal RESOLVED/GREEN state. In
        # production this would be reached by a manager approving the
        # pending review and the recipe running to completion.
        exception_store.update(
            exc_id,
            "tenant-a",
            lifecycle_state="RESOLVED",
            shadow_verdict="GREEN",
            final_status="COMPLETE",
        )
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/reanalyze",
            json={"reason": "curious"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "INVALID_STATE"

    def test_reanalyze_rate_limit_enforced(self, client, analyst_token, manager_token):
        from contracts.policy import REANALYSIS_MAX_ATTEMPTS
        exc_id = self._create_pending_review(client, analyst_token)
        # Exhaust attempts.
        for i in range(REANALYSIS_MAX_ATTEMPTS):
            r = client.post(
                f"/api/v1/exceptions/{exc_id}/reanalyze",
                json={"reason": f"attempt {i + 1}"},
                headers=_auth(manager_token),
            )
            assert r.status_code == 200, r.json()
        # Next call must be rejected with 429.
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/reanalyze",
            json={"reason": "one more"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "RATE_LIMITED"

    def test_reanalyze_reason_is_required(self, client, analyst_token, manager_token):
        exc_id = self._create_pending_review(client, analyst_token)
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/reanalyze",
            json={},  # missing reason
            headers=_auth(manager_token),
        )
        assert r.status_code == 422

    def test_reanalyze_writes_sox_audit_entry(self, client, analyst_token, manager_token):
        exc_id = self._create_pending_review(client, analyst_token)
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/reanalyze",
            json={"reason": "Buyer provided new info"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        audit = exception_store.get_audit_log("tenant-a")
        reanalysis_events = [e for e in audit if e["policy_key"] == "EXCEPTION_REANALYZE"]
        assert len(reanalysis_events) == 1
        assert reanalysis_events[0]["change_reason"] == "Buyer provided new info"

    def test_reanalyze_tenant_isolation(self, client, analyst_token, manager_token, tenant_b_token):
        exc_id = self._create_pending_review(client, analyst_token)
        # Manager from tenant-b must not be able to reanalyze tenant-a's exception.
        tenant_b_manager = create_test_token(roles=["manager"], org="tenant-b")
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/reanalyze",
            json={"reason": "cross-tenant"},
            headers=_auth(tenant_b_manager),
        )
        assert r.status_code == 404


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

    # -----------------------------------------------------------------
    # Review L2 — enrichment adapter: recipe output → typed analysis
    # field. Proves the adapter registry projects the expected shape
    # end-to-end (POST /resolve → GET /analysis) for every wired
    # recipe. Adding a new adapter = add a case here.
    # -----------------------------------------------------------------

    def _create_phr(self, client, token, *, po_price: float) -> str:
        """Post an EDI_850_PRICE_HOLD event and return its exception_id."""
        event = {
            "order_id": f"PHR-{int(po_price * 100)}",
            "line_item": 1,
            "po_price": po_price,
            "sap_base_price": 100.0,
            "event_type": "EDI_850_PRICE_HOLD",
            "retailer_id": "R-10",
            "line_count": 1,
            "metadata": {"price_hold_status": "HELD"},
        }
        r = client.post(
            "/api/v1/exceptions/resolve", json=event, headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        return r.json()["exception_id"]

    def test_analysis_carries_price_hold_enrichment(self, client, analyst_token):
        exc_id = self._create_phr(client, analyst_token, po_price=101.0)
        r = client.get(
            f"/api/v1/exceptions/{exc_id}/analysis",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data.get("price_hold_analysis") is not None
        phr = data["price_hold_analysis"]
        # AUTO_RELEASE branch: 1% variance < 2% tolerance
        assert phr["hold_status"] == "RELEASED"
        assert phr["action"] == "AUTO_RELEASE"
        assert phr["po_price"] == 101.0
        assert phr["sap_base_price"] == 100.0
        assert phr["variance_pct"] == pytest.approx(0.01, abs=1e-6)
        assert phr["tolerance_pct"] > 0
        assert phr["hard_block_pct"] > phr["tolerance_pct"]
        assert phr["reason"]  # non-empty human-readable string

    def test_analysis_price_hold_escalate_branch(self, client, analyst_token):
        # 5% variance — above 2% tolerance, below 10% hard-block → ESCALATE.
        # Hold remains in place (status=REVIEW_REQUIRED maps to hold_status=HELD).
        exc_id = self._create_phr(client, analyst_token, po_price=105.0)
        r = client.get(
            f"/api/v1/exceptions/{exc_id}/analysis",
            headers=_auth(analyst_token),
        )
        data = r.json()
        phr = data["price_hold_analysis"]
        assert phr["hold_status"] == "HELD"
        assert phr["action"] == "ESCALATE"

    def test_analysis_price_hold_hard_block_branch(self, client, analyst_token):
        # 15% variance → HARD_BLOCK. Status=REJECTED maps to hold_status=HELD.
        exc_id = self._create_phr(client, analyst_token, po_price=115.0)
        r = client.get(
            f"/api/v1/exceptions/{exc_id}/analysis",
            headers=_auth(analyst_token),
        )
        data = r.json()
        phr = data["price_hold_analysis"]
        assert phr["hold_status"] == "HELD"
        assert phr["action"] == "HARD_BLOCK"

    def _create_edi_mismatch(self, client, token, *, sub_type: str) -> str:
        event = {
            "order_id": f"EDI-{sub_type}",
            "line_item": 1,
            "po_price": 100.0,
            "sap_base_price": 100.0,
            "event_type": "EDI_850_LINE_MISMATCH",
            "retailer_id": "R-20",
            "line_count": 1,
            "metadata": {
                "mismatch_sub_type": sub_type,
                "expected_value": "SKU-A",
                "received_value": "SKU-B",
            },
        }
        r = client.post(
            "/api/v1/exceptions/resolve", json=event, headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        return r.json()["exception_id"]

    def test_analysis_carries_edi_mismatch_enrichment(self, client, analyst_token):
        exc_id = self._create_edi_mismatch(client, analyst_token, sub_type="QTY_MISMATCH")
        r = client.get(
            f"/api/v1/exceptions/{exc_id}/analysis",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        edi = r.json().get("edi_mismatch_analysis")
        assert edi is not None
        assert edi["sub_type"] == "QTY_MISMATCH"
        assert edi["classification"] == "REVIEW"
        assert edi["recommended_action"] == "REQUEST_BUYER_CONFIRMATION"
        assert edi["autonomy_level"] in {"L1", "L2", "L3"}
        assert edi["notification_template"] == "edi_line_mismatch_inquiry"

    def test_analysis_edi_sku_mismatch_hard_reject(self, client, analyst_token):
        exc_id = self._create_edi_mismatch(client, analyst_token, sub_type="SKU_MISMATCH")
        data = client.get(
            f"/api/v1/exceptions/{exc_id}/analysis",
            headers=_auth(analyst_token),
        ).json()
        edi = data["edi_mismatch_analysis"]
        assert edi["classification"] == "HARD_REJECT"
        assert edi["recommended_action"] == "BLOCK_AND_NOTIFY"

    def test_analysis_edi_price_mismatch_does_not_surface(self, client, analyst_token):
        # PRICE_MISMATCH routes at classifier time to CONTRACTUAL_CORRECTION
        # (PriceAdjustmentRecipe, not EdiMismatchRecipe). No PriceAdjustment
        # adapter is wired yet, so neither enrichment field appears.
        exc_id = self._create_edi_mismatch(
            client, analyst_token, sub_type="PRICE_MISMATCH",
        )
        data = client.get(
            f"/api/v1/exceptions/{exc_id}/analysis",
            headers=_auth(analyst_token),
        ).json()
        assert data.get("edi_mismatch_analysis") is None
        # Classifier fork verification — intent landed as CONTRACTUAL_CORRECTION.
        detail = client.get(
            f"/api/v1/exceptions/{exc_id}", headers=_auth(analyst_token),
        ).json()
        assert detail["intent"] == "CONTRACTUAL_CORRECTION"

    def test_analysis_omits_enrichment_when_no_recipe(self, client, analyst_token):
        # Baseline /resolve event carries no recipe-specific intent, so
        # neither enrichment field should appear. Protects the data-presence
        # pattern — absent ≠ null ≠ partial.
        exc_id = self._create_exception(client, analyst_token)
        data = client.get(
            f"/api/v1/exceptions/{exc_id}/analysis",
            headers=_auth(analyst_token),
        ).json()
        assert data.get("price_hold_analysis") is None
        assert data.get("edi_mismatch_analysis") is None

    # -----------------------------------------------------------------
    # Verdict Pillar 2.3 — structured trace surface for audit gaps.
    # When build_analysis flagged AUDIT_CONTEXT_MISSING, the trace
    # must carry the class + ordered list of missing fields so the
    # auditor doesn't regex the prose explanation.
    # -----------------------------------------------------------------

    def test_trace_carries_audit_missing_fields_on_complete_records(
        self, client, analyst_token,
    ):
        """Happy path: PHR AUTO_RELEASE → coverage complete → the
        trace's audit_context_missing_* fields are absent/empty."""
        exc_id = self._create_phr(client, analyst_token, po_price=101.0)
        trace = client.get(
            f"/api/v1/exceptions/{exc_id}/trace",
            headers=_auth(analyst_token),
        ).json()
        assert trace.get("audit_context_missing_class") in (None, "")
        assert trace.get("audit_context_missing_fields") == []

    # -----------------------------------------------------------------
    # L2d.delivery_delay — adapter end-to-end. Three-path lookup
    # (GREEN → adapter projects recipe output; YELLOW → composer
    # synthesises via pure recipe invocation; missing dates → None).
    # -----------------------------------------------------------------

    def _create_delivery_delay(
        self, client, token, *, days_late: int,
    ) -> str:
        event = {
            "order_id": f"PO-DD-{days_late}",
            "line_item": 1,
            "po_price": 10.0,
            "sap_base_price": 10.0,
            "event_type": "DELIVERY_DELAY",
            "retailer_id": "R-74",
            "line_count": 3,
            "metadata": {
                "planned_date": "2026-04-20T00:00:00Z",
                "projected_eta": f"2026-04-{20 + days_late:02d}T00:00:00Z",
                "days_late": days_late,
                "delay_category": "CARRIER_DELAY",
                "delay_reason": "Carrier hub backlog (decorative).",
                "carrier": "ACME-FRT",
                "route": "MID-SE-01",
            },
        }
        r = client.post(
            "/api/v1/exceptions/resolve", json=event, headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        return r.json()["exception_id"]

    def test_analysis_delivery_delay_shadow_gated_suppresses_projection(
        self, client, analyst_token,
    ):
        """T5: post delivery_delay_financial_gap retirement, the
        shadow-gated path doesn't fetch sla_contract gateway —
        at_risk + sla_deadline absent → composer suppresses
        projection (no partial-truth state)."""
        # 3 days late → MINOR_DELAY → YELLOW shadow.
        exc_id = self._create_delivery_delay(client, analyst_token, days_late=3)
        data = client.get(
            f"/api/v1/exceptions/{exc_id}/analysis",
            headers=_auth(analyst_token),
        ).json()
        assert data.get("delivery_delay_analysis") is None

    def test_analysis_delivery_delay_severe_shadow_gated_suppresses_projection(
        self, client, analyst_token,
    ):
        """7 days late → SEVERE_DELAY → RED shadow → projection
        suppressed (gateway data missing)."""
        exc_id = self._create_delivery_delay(client, analyst_token, days_late=7)
        data = client.get(
            f"/api/v1/exceptions/{exc_id}/analysis",
            headers=_auth(analyst_token),
        ).json()
        assert data.get("delivery_delay_analysis") is None

    def test_trace_for_delivery_delay_records_missing_gateway_fields(
        self, client, analyst_token,
    ):
        """T5: post delivery_delay_financial_gap retirement, the
        shadow-gated path skips resolve_dependencies → at_risk +
        sla_deadline surface in the audit-gap trace."""
        exc_id = self._create_delivery_delay(client, analyst_token, days_late=3)
        trace = client.get(
            f"/api/v1/exceptions/{exc_id}/trace",
            headers=_auth(analyst_token),
        ).json()
        missing = trace.get("audit_context_missing_fields") or []
        assert "at_risk" in missing
        assert "sla_deadline" in missing

    # -----------------------------------------------------------------
    # L2d.overmax — adapter end-to-end. Recipe computes excess_qty,
    # exceedance_pct, at_risk, trim_plan from event metadata.
    # contract_ref/block_status/block_reason are grandfathered.
    # -----------------------------------------------------------------

    def _create_overmax(
        self, client, token, *, total_ordered: float, max_qty: float,
    ) -> str:
        event = {
            "order_id": f"PO-OM-{int(total_ordered)}",
            "line_item": 1,
            "po_price": 10.0,
            "sap_base_price": 10.0,
            "event_type": "OVER_MAX_QTY",
            "retailer_id": "R-30",
            "line_count": 2,
            "metadata": {
                "total_ordered": total_ordered,
                "max_qty": max_qty,
                "uom": "CASE",
                "order_lines": [
                    {
                        "sku": "SKU-A", "description": "Widget A",
                        "qty": total_ordered / 2,
                        "max_line_qty": max_qty / 2,
                        "is_even_layer_item": True,
                    },
                    {
                        "sku": "SKU-B", "description": "Widget B",
                        "qty": total_ordered / 2,
                        "max_line_qty": max_qty / 2,
                        "is_even_layer_item": True,
                    },
                ],
                "unit_cost_per_line": {"SKU-A": 12.0, "SKU-B": 18.0},
            },
        }
        r = client.post(
            "/api/v1/exceptions/resolve", json=event, headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        return r.json()["exception_id"]

    def test_analysis_overmax_shadow_gated_suppresses_projection(
        self, client, analyst_token,
    ):
        """T5: post overmax_gateway_gap retirement, the shadow-gated
        path doesn't fetch sap_contract / sap_block — contract_ref /
        block_status / block_reason absent → composer suppresses
        projection."""
        # 110 vs 100 ceiling = 10% exceedance → MINOR → YELLOW shadow.
        exc_id = self._create_overmax(
            client, analyst_token, total_ordered=110.0, max_qty=100.0,
        )
        data = client.get(
            f"/api/v1/exceptions/{exc_id}/analysis",
            headers=_auth(analyst_token),
        ).json()
        assert data.get("overmax_analysis") is None

    def test_trace_for_overmax_shadow_gated_records_missing_gateway_fields(
        self, client, analyst_token,
    ):
        """T5: post overmax_gateway_gap retirement, shadow-gated paths
        skip resolve_dependencies — gateway-sourced contract_ref /
        block_status / block_reason are absent and surface in the
        audit-gap trace. Architectural follow-up (move gateway READS
        before shadow) tracked separately."""
        exc_id = self._create_overmax(
            client, analyst_token, total_ordered=110.0, max_qty=100.0,
        )
        trace = client.get(
            f"/api/v1/exceptions/{exc_id}/trace",
            headers=_auth(analyst_token),
        ).json()
        missing = trace.get("audit_context_missing_fields") or []
        assert "contract_ref" in missing
        assert "block_status" in missing
        assert "block_reason" in missing

    # -----------------------------------------------------------------
    # L2d.moq — adapter end-to-end. Recipe computes shortfall + plan;
    # at_risk surfaces the recipe's uplift_value.
    # -----------------------------------------------------------------

    def _create_moq(
        self, client, token, *, ordered: float, moq: float,
    ) -> str:
        event = {
            "order_id": f"PO-MOQ-{int(ordered)}",
            "line_item": 1,
            "po_price": 10.0,
            "sap_base_price": 10.0,
            "event_type": "MIN_ORDER_QTY",
            "retailer_id": "R-40",
            "line_count": 1,
            "metadata": {
                "sku": "SKU-MQ-001",
                "ordered_qty": ordered,
                "moq_qty": moq,
                "unit_cost": 12.5,
                "uom": "CASE",
            },
        }
        r = client.post(
            "/api/v1/exceptions/resolve", json=event, headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        return r.json()["exception_id"]

    def test_analysis_moq_shadow_gated_suppresses_projection(
        self, client, analyst_token,
    ):
        """T5: post moq_gateway_gap retirement, the shadow-gated MOQ
        path doesn't fetch sap_customer_master / sap_contract /
        sap_block — the projection is suppressed by the audit-coverage
        gate to avoid partial-truth state."""
        # Ordered 18 vs MOQ 20 → 10% shortfall → YELLOW shadow.
        exc_id = self._create_moq(client, analyst_token, ordered=18.0, moq=20.0)
        data = client.get(
            f"/api/v1/exceptions/{exc_id}/analysis",
            headers=_auth(analyst_token),
        ).json()
        # No moq_analysis — composer suppresses projections that fail
        # audit-bearing coverage.
        assert data.get("moq_analysis") is None

    def test_trace_for_moq_shadow_gated_records_missing_gateway_fields(
        self, client, analyst_token,
    ):
        """T5: post moq_gateway_gap retirement, the audit-gap trace
        surfaces moq_source / channel / contract_ref / block_status
        on shadow-gated paths."""
        exc_id = self._create_moq(client, analyst_token, ordered=18.0, moq=20.0)
        trace = client.get(
            f"/api/v1/exceptions/{exc_id}/trace",
            headers=_auth(analyst_token),
        ).json()
        missing = trace.get("audit_context_missing_fields") or []
        assert "moq_source" in missing
        assert "channel" in missing
        assert "contract_ref" in missing
        assert "block_status" in missing

    # -----------------------------------------------------------------
    # L2d.pallet — recipe + UI shapes are 1:1 (sku/desc/uom/layer_qty/
    # pallet_qty/...). Adapter is pure coercion.
    # -----------------------------------------------------------------

    def _create_pallet(self, client, token) -> str:
        event = {
            "order_id": "PO-PLT-001",
            "line_item": 1,
            "po_price": 10.0,
            "sap_base_price": 10.0,
            "event_type": "PALLET_CONFIG_VIOLATION",
            "retailer_id": "R-50",
            "line_count": 2,
            "metadata": {
                "pallet_lines": [
                    {
                        "sku": "SKU-PLT-A", "description": "Widget A",
                        "uom": "CASE",
                        "layer_qty": 10, "pallet_qty": 60,
                        "ordered_qty": 75,
                    },
                    {
                        "sku": "SKU-PLT-B", "description": "Widget B",
                        "uom": "CASE",
                        "layer_qty": 12, "pallet_qty": 72,
                        "ordered_qty": 60,
                    },
                ],
            },
        }
        r = client.post(
            "/api/v1/exceptions/resolve", json=event, headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        return r.json()["exception_id"]

    def test_analysis_carries_pallet_alignment(self, client, analyst_token):
        exc_id = self._create_pallet(client, analyst_token)
        data = client.get(
            f"/api/v1/exceptions/{exc_id}/analysis",
            headers=_auth(analyst_token),
        ).json()
        pa = data.get("pallet_analysis")
        assert pa is not None
        assert pa["order_line_count"] == 2
        assert len(pa["lines"]) == 2
        assert pa["lines"][0]["sku"] == "SKU-PLT-A"
        assert pa["classification"] in (
            "BROKEN_LAYER", "PARTIAL_PALLET", "MIXED_VIOLATION",
        )
        assert len(pa["suggested_plan"]) == 2

    def test_trace_for_pallet_has_no_audit_gap(self, client, analyst_token):
        exc_id = self._create_pallet(client, analyst_token)
        trace = client.get(
            f"/api/v1/exceptions/{exc_id}/trace",
            headers=_auth(analyst_token),
        ).json()
        assert trace.get("audit_context_missing_fields") == []
