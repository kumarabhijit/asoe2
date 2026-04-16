"""Auth flow tests — multi-step login, SSO, MFA, token refresh, RBAC.

Validates the SAP Concur-style login flow:
  Step 1: Email/password → MFA challenge (POST /api/auth/login)
  Step 2: MFA verification → JWT tokens (POST /api/auth/mfa/verify)

Also covers: SSO initiation/callback, token refresh with rotation,
GET /api/auth/me, environment isolation, and all RBAC role checks.

Architecture_v3.md Section 11.1, 11.2, 11.6.
"""

from __future__ import annotations

import pytest

from api.deps import (
    _get_jwt_secret,
    _jwt_decode,
    _jwt_encode,
    create_access_token,
    create_test_token,
    create_refresh_token,
)
from tests.sandbox.conftest import auth_header, SANDBOX_TENANT


# ═══════════════════════════════════════════════════════════════════════════
# Multi-step login flow (Step 1: email → Step 2: MFA)
# ═══════════════════════════════════════════════════════════════════════════

class TestMultiStepLoginFlow:
    """POST /api/auth/login followed by POST /api/auth/mfa/verify."""

    def test_step1_login_returns_tokens_for_known_user(self, client):
        """Login with known user email returns tokens + user profile directly."""
        resp = client.post(
            "/api/auth/login",
            json={"email": "marcus.webb@acme-corp.com", "password": "password"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["access_token"] != ""
        assert data["refresh_token"]
        assert data["user"]["name"] == "Marcus Webb"
        assert data["user"]["roles"] == ["admin"]

    def test_step1_login_rejects_unknown_user(self, client):
        """Login with unknown email returns 401."""
        resp = client.post(
            "/api/auth/login",
            json={"email": "admin@asoe.test", "password": "test-password"},
        )
        assert resp.status_code == 401

    def test_step2_mfa_verify_returns_tokens(self, client):
        """MFA verify still works (backward compat stub)."""
        resp = client.post(
            "/api/auth/mfa/verify",
            json={"mfa_token": "any-token", "code": "123456"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["access_token"]
        assert data["refresh_token"]
        assert data["token_type"] == "bearer"
        assert data["user"]["roles"] == ["admin"]
        assert "permissions" in data["user"]
        assert "visible_tabs" in data["user"]

    def test_full_flow_token_works_for_api_calls(self, client):
        """End-to-end: login → use access_token for API."""
        # Login directly (no MFA step needed for known users)
        login_resp = client.post(
            "/api/auth/login",
            json={"email": "marcus.webb@acme-corp.com", "password": "password"},
        )
        assert login_resp.status_code == 200
        access_token = login_resp.json()["access_token"]

        # Use the token to access a protected endpoint
        resp = client.get("/api/auth/me", headers=auth_header(access_token))
        assert resp.status_code == 200
        assert resp.json()["email"] == "marcus.webb@acme-corp.com"
        assert "visible_tabs" in resp.json()

    def test_login_rejects_empty_credentials(self, client):
        resp = client.post(
            "/api/auth/login",
            json={"email": "", "password": ""},
        )
        assert resp.status_code == 400

    def test_mfa_verify_rejects_empty_code(self, client):
        resp = client.post(
            "/api/auth/mfa/verify",
            json={"mfa_token": "some-token", "code": ""},
        )
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# SSO flow (architecture_v3.md Section 11.1)
# ═══════════════════════════════════════════════════════════════════════════

class TestSSOFlow:
    """POST /api/auth/sso/init and GET /api/auth/sso/callback."""

    def test_sso_init_returns_redirect_url(self, client):
        resp = client.post("/api/auth/sso/init")
        assert resp.status_code == 200
        data = resp.json()
        assert "redirect_url" in data
        assert data["redirect_url"].startswith("https://")

    def test_sso_callback_returns_tokens(self, client):
        resp = client.get("/api/auth/sso/callback")
        assert resp.status_code == 200
        data = resp.json()
        assert data["access_token"]
        assert data["refresh_token"]
        assert data["token_type"] == "bearer"

    def test_sso_callback_token_is_valid(self, client):
        """Token from SSO callback can be used for API calls."""
        callback_resp = client.get("/api/auth/sso/callback")
        token = callback_resp.json()["access_token"]
        resp = client.get("/api/auth/me", headers=auth_header(token))
        assert resp.status_code == 200
        assert resp.json()["email"] == "sso@example.com"


# ═══════════════════════════════════════════════════════════════════════════
# Token refresh with rotation (architecture_v3.md Section 11.1)
# ═══════════════════════════════════════════════════════════════════════════

class TestTokenRefresh:
    """POST /api/auth/refresh — rotates both access and refresh tokens."""

    def test_refresh_returns_new_tokens(self, client):
        refresh = create_refresh_token(
            sub="test-user", email="test@asoe.test", name="Test",
            roles=["analyst"], org=SANDBOX_TENANT,
        )
        resp = client.post(
            "/api/auth/refresh",
            json={"refresh_token": refresh},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["access_token"]
        assert data["refresh_token"]
        # New access token should be valid
        assert len(data["access_token"]) > 20

    def test_refresh_rejects_access_token(self, client, analyst_token):
        """Cannot use an access token as a refresh token."""
        resp = client.post(
            "/api/auth/refresh",
            json={"refresh_token": analyst_token},
        )
        assert resp.status_code == 401

    def test_refresh_rejects_invalid_token(self, client):
        resp = client.post(
            "/api/auth/refresh",
            json={"refresh_token": "invalid.token.here"},
        )
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# GET /api/auth/me — current user profile
# ═══════════════════════════════════════════════════════════════════════════

class TestCurrentUser:
    """GET /api/auth/me — returns authenticated user profile."""

    def test_me_returns_user_profile(self, client, analyst_token):
        resp = client.get("/api/auth/me", headers=auth_header(analyst_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["sub"] == "analyst-user"
        assert data["email"] == "analyst@asoe.test"
        assert "analyst" in data["roles"]
        assert data["org"] == SANDBOX_TENANT
        assert "permissions" in data

    def test_me_requires_auth(self, client):
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# Environment isolation (architecture_v3.md Section 11.6)
# ═══════════════════════════════════════════════════════════════════════════

class TestEnvironmentIsolation:
    """Tokens with mismatched env claim are rejected."""

    def test_production_token_rejected_in_sandbox(self, client):
        """A token with env=production is rejected when ASOE_ENV=sandbox."""
        import os
        old = os.environ.get("ASOE_ENV")
        os.environ["ASOE_ENV"] = "sandbox"
        try:
            token = create_test_token(
                roles=["analyst"], org=SANDBOX_TENANT, env="production",
            )
            resp = client.get("/api/auth/me", headers=auth_header(token))
            assert resp.status_code == 403
        finally:
            if old is None:
                os.environ.pop("ASOE_ENV", None)
            else:
                os.environ["ASOE_ENV"] = old


# ═══════════════════════════════════════════════════════════════════════════
# RBAC enforcement across all endpoints
# ═══════════════════════════════════════════════════════════════════════════

class TestRBACEnforcement:
    """Verify role requirements for each protected endpoint."""

    def test_viewer_cannot_resolve(self, client, viewer_token):
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json={"order_id": "X", "po_price": 1, "sap_base_price": 1},
            headers=auth_header(viewer_token),
        )
        assert resp.status_code == 403

    def test_viewer_can_read_exceptions(self, client, analyst_token, viewer_token):
        # Create an exception first
        client.post(
            "/api/v1/exceptions/resolve",
            json={"order_id": "X", "po_price": 90, "sap_base_price": 100,
                   "retailer_id": "R-01"},
            headers=auth_header(analyst_token),
        )
        resp = client.get(
            "/api/v1/exceptions",
            headers=auth_header(viewer_token),
        )
        assert resp.status_code == 200

    def test_analyst_cannot_override(self, client, analyst_token):
        # Create exception
        res = client.post(
            "/api/v1/exceptions/resolve",
            json={"order_id": "X", "po_price": 90, "sap_base_price": 100,
                   "retailer_id": "R-01"},
            headers=auth_header(analyst_token),
        )
        exc_id = res.json()["exception_id"]
        resp = client.patch(
            f"/api/v1/exceptions/{exc_id}/override",
            json={"action": "OVERRIDE", "notes": "", "resolved_by": "analyst"},
            headers=auth_header(analyst_token),
        )
        assert resp.status_code == 403

    def test_partner_can_read_exceptions(self, client, partner_token):
        resp = client.get(
            "/api/v1/exceptions",
            headers=auth_header(partner_token),
        )
        assert resp.status_code == 200

    def test_partner_cannot_resolve(self, client, partner_token):
        resp = client.post(
            "/api/v1/exceptions/resolve",
            json={"order_id": "X", "po_price": 1, "sap_base_price": 1},
            headers=auth_header(partner_token),
        )
        assert resp.status_code == 403

    def test_expired_token_rejected(self, client):
        import time
        payload = {
            "sub": "expired-user", "email": "x@x.com", "name": "X",
            "roles": ["analyst"], "org": SANDBOX_TENANT, "env": "sandbox",
            "exp": int(time.time()) - 3600, "token_type": "access",
        }
        token = _jwt_encode(payload, _get_jwt_secret())
        resp = client.get("/api/auth/me", headers=auth_header(token))
        assert resp.status_code == 401
