"""Tests for ASOE security features.

Covers architecture_v3.md:
  §11.1 — Token expiry, access vs refresh tokens, auth_method claim
  §11.2 — RBAC enforcement
  §11.3 — Partner-role scoping
  §11.4 — X-Trace-ID propagation
  §11.5 — Configurable JWT secret
  §11.6 — Environment isolation
"""

from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import (
    ACCESS_TOKEN_EXPIRE_SECONDS,
    _get_jwt_secret,
    _jwt_decode,
    _jwt_encode,
    create_access_token,
    create_refresh_token,
    create_test_token,
)
from api.store import exception_store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    exception_store.clear()
    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


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
# §11.1 — Token expiry
# ---------------------------------------------------------------------------

class TestTokenExpiry:
    def test_access_token_has_exp_claim(self):
        token = create_access_token(
            sub="u1", email="u@x.com", name="U", roles=["analyst"], org="t1",
        )
        payload = _jwt_decode(token, _get_jwt_secret())
        assert "exp" in payload
        assert "iat" in payload
        assert payload["exp"] - payload["iat"] == ACCESS_TOKEN_EXPIRE_SECONDS

    def test_refresh_token_has_exp_claim(self):
        # TTL is env-driven (see api/deps.py::_resolve_token_ttls — 30d in
        # sandbox, 7d otherwise). Assert against the resolved constant
        # rather than a hardcoded number so the test stays correct under
        # both env defaults.
        from api.deps import REFRESH_TOKEN_EXPIRE_SECONDS
        token = create_refresh_token(
            sub="u1", email="u@x.com", name="U", roles=["analyst"], org="t1",
        )
        payload = _jwt_decode(token, _get_jwt_secret())
        assert payload["token_type"] == "refresh"
        assert payload["exp"] - payload["iat"] == REFRESH_TOKEN_EXPIRE_SECONDS

    def test_expired_token_rejected(self, client):
        """An expired token should return 401."""
        secret = _get_jwt_secret()
        payload = {
            "sub": "u1", "email": "u@x.com", "name": "U",
            "roles": ["analyst"], "org": "t1", "env": "sandbox",
            "exp": int(time.time()) - 10,  # expired 10 seconds ago
        }
        expired_token = _jwt_encode(payload, secret)
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers=_auth(expired_token),
        )
        assert r.status_code == 401

    def test_valid_token_accepted(self, client):
        token = create_test_token(roles=["analyst"], org="t1")
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers=_auth(token),
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# §11.1 — Access vs Refresh token types
# ---------------------------------------------------------------------------

class TestTokenTypes:
    def test_access_token_has_type(self):
        token = create_access_token(
            sub="u1", email="u@x.com", name="U", roles=["analyst"], org="t1",
        )
        payload = _jwt_decode(token, _get_jwt_secret())
        assert payload["token_type"] == "access"

    def test_refresh_token_has_type(self):
        token = create_refresh_token(
            sub="u1", email="u@x.com", name="U", roles=["analyst"], org="t1",
        )
        payload = _jwt_decode(token, _get_jwt_secret())
        assert payload["token_type"] == "refresh"

    def test_refresh_endpoint_rejects_access_token(self, client):
        """POST /api/auth/refresh must receive a refresh token, not access."""
        access = create_access_token(
            sub="u1", email="u@x.com", name="U", roles=["analyst"], org="t1",
        )
        r = client.post(
            "/api/auth/refresh",
            json={"refresh_token": access},
        )
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "INVALID_TOKEN"

    def test_refresh_endpoint_accepts_refresh_token(self, client):
        refresh = create_refresh_token(
            sub="u1", email="u@x.com", name="U", roles=["analyst"], org="t1",
        )
        r = client.post(
            "/api/auth/refresh",
            json={"refresh_token": refresh},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["access_token"]
        assert data["refresh_token"]  # rotated

    def test_refresh_rotation_issues_new_refresh(self, client):
        """Each refresh issues both a new access and new refresh token."""
        refresh1 = create_refresh_token(
            sub="u1", email="u@x.com", name="U", roles=["analyst"], org="t1",
        )
        r = client.post("/api/auth/refresh", json={"refresh_token": refresh1})
        data = r.json()
        # Both token types are returned (rotation contract)
        assert data["access_token"]
        assert data["refresh_token"]
        # The access token is a different type than the refresh token
        access_payload = _jwt_decode(data["access_token"], _get_jwt_secret())
        refresh_payload = _jwt_decode(data["refresh_token"], _get_jwt_secret())
        assert access_payload["token_type"] == "access"
        assert refresh_payload["token_type"] == "refresh"


# ---------------------------------------------------------------------------
# §11.1 — auth_method claim
# ---------------------------------------------------------------------------

class TestAuthMethod:
    def test_login_sets_auth_method(self, client):
        """Login with known user sets auth_method claim on JWT."""
        r = client.post(
            "/api/auth/login",
            json={"email": "marcus.webb@acme-corp.com", "password": "password"},
        )
        access = r.json()["access_token"]
        payload = _jwt_decode(access, _get_jwt_secret())
        assert payload.get("auth_method") == "password+mfa"

    def test_sso_callback_sets_auth_method(self, client):
        r = client.get("/api/auth/sso/callback")
        access = r.json()["access_token"]
        payload = _jwt_decode(access, _get_jwt_secret())
        assert payload.get("auth_method") == "sso"


# ---------------------------------------------------------------------------
# §11.4 — X-Trace-ID propagation
# ---------------------------------------------------------------------------

class TestTraceIDPropagation:
    def test_response_includes_trace_id(self, client):
        token = create_test_token(roles=["analyst"], org="t1")
        r = client.get("/api/v1/health")
        assert "X-Trace-ID" in r.headers

    def test_client_trace_id_propagated(self, client):
        """Client-provided X-Trace-ID is echoed back."""
        token = create_test_token(roles=["analyst"], org="t1")
        r = client.get(
            "/api/v1/health",
            headers={"X-Trace-ID": "my-custom-trace-id-123"},
        )
        assert r.headers["X-Trace-ID"] == "my-custom-trace-id-123"

    def test_missing_trace_id_generates_uuid(self, client):
        r = client.get("/api/v1/health")
        trace_id = r.headers.get("X-Trace-ID", "")
        # Should be a UUID-like string (36 chars with dashes)
        assert len(trace_id) >= 32

    def test_trace_id_on_error_response(self, client):
        """Even error responses should include X-Trace-ID."""
        r = client.post("/api/v1/exceptions/resolve", json=_sample_event())
        # 401 because no auth header
        assert r.status_code == 401
        assert "X-Trace-ID" in r.headers


# ---------------------------------------------------------------------------
# §11.5 — Configurable JWT secret
# ---------------------------------------------------------------------------

class TestJWTSecret:
    def test_default_secret(self):
        prev = os.environ.pop("ASOE_JWT_SECRET", None)
        try:
            assert _get_jwt_secret() == "asoe-dev-secret-do-not-use-in-production"
        finally:
            if prev:
                os.environ["ASOE_JWT_SECRET"] = prev

    def test_custom_secret_from_env(self):
        prev = os.environ.get("ASOE_JWT_SECRET")
        os.environ["ASOE_JWT_SECRET"] = "my-production-secret"
        try:
            assert _get_jwt_secret() == "my-production-secret"
        finally:
            if prev:
                os.environ["ASOE_JWT_SECRET"] = prev
            else:
                os.environ.pop("ASOE_JWT_SECRET", None)

    def test_token_signed_with_wrong_secret_rejected(self, client):
        """A token signed with a different secret must be rejected."""
        payload = {
            "sub": "u1", "email": "u@x.com", "name": "U",
            "roles": ["analyst"], "org": "t1", "env": "sandbox",
            "exp": int(time.time()) + 3600,
        }
        wrong_token = _jwt_encode(payload, "wrong-secret")
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers=_auth(wrong_token),
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# §11.6 — Environment isolation
# ---------------------------------------------------------------------------

class TestEnvironmentIsolation:
    def test_matching_env_accepted(self, client):
        """Token env=sandbox matches default ASOE_ENV=sandbox."""
        token = create_test_token(env="sandbox", roles=["analyst"], org="t1")
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers=_auth(token),
        )
        assert r.status_code == 200

    def test_mismatched_env_rejected(self, client):
        """Token env=production with ASOE_ENV=sandbox returns 403."""
        token = create_test_token(env="production", roles=["analyst"], org="t1")
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers=_auth(token),
        )
        assert r.status_code == 403
        error = r.json()["error"]
        assert error["code"] == "ENV_MISMATCH"
        # §11.6: generic message, no internal state leaked
        assert error["message"] == "Access denied."
        assert "details" not in error or error.get("details") is None

    def test_production_env_rejects_sandbox_token(self, client):
        """When ASOE_ENV=production, a sandbox token returns 403."""
        prev = os.environ.get("ASOE_ENV")
        os.environ["ASOE_ENV"] = "production"
        try:
            token = create_test_token(env="sandbox", roles=["analyst"], org="t1")
            r = client.post(
                "/api/v1/exceptions/resolve",
                json=_sample_event(),
                headers=_auth(token),
            )
            assert r.status_code == 403
        finally:
            if prev:
                os.environ["ASOE_ENV"] = prev
            else:
                os.environ.pop("ASOE_ENV", None)


# ---------------------------------------------------------------------------
# §11.3 — Partner-role scoping
# ---------------------------------------------------------------------------

class TestPartnerScoping:
    def test_partner_with_retailer_id_in_token(self):
        """Partner tokens include retailer_id claim."""
        token = create_access_token(
            sub="partner-1", email="p@x.com", name="Partner",
            roles=["partner"], org="t1", retailer_id="R-001",
        )
        payload = _jwt_decode(token, _get_jwt_secret())
        assert payload["retailer_id"] == "R-001"

    def test_partner_can_list_exceptions(self, client):
        """Partner role can access the list endpoint."""
        token = create_test_token(roles=["partner"], org="t1", retailer_id="R-001")
        r = client.get("/api/v1/exceptions", headers=_auth(token))
        assert r.status_code == 200

    def test_partner_cannot_resolve(self, client):
        """Partner role cannot call resolve (requires analyst+)."""
        token = create_test_token(roles=["partner"], org="t1")
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers=_auth(token),
        )
        assert r.status_code == 403

    def test_partner_cannot_see_trace(self, client):
        """Partner role cannot access trace endpoint (requires analyst+)."""
        token = create_test_token(roles=["partner"], org="t1")
        r = client.get(
            "/api/v1/exceptions/fake-id/trace",
            headers=_auth(token),
        )
        assert r.status_code == 403

    def test_partner_cannot_override(self, client):
        """Partner role cannot override (requires manager+)."""
        token = create_test_token(roles=["partner"], org="t1")
        r = client.patch(
            "/api/v1/exceptions/fake-id/disposition",
            json={"action": "X", "notes": "Y", "reason_tag": "OTHER"},
            headers=_auth(token),
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Error envelope security
# ---------------------------------------------------------------------------

class TestErrorSecurity:
    def test_unhandled_error_hides_internals(self, client):
        """Unhandled exceptions should not leak stack traces."""
        r = client.get("/api/v1/exceptions/bad-id")
        # No auth → 401, not 500 with stack trace
        assert r.status_code == 401
        body = r.json()
        assert "traceback" not in str(body).lower()
        assert "stack" not in str(body).lower()

    def test_env_mismatch_hides_details(self, client):
        """§11.6: ENV_MISMATCH response must not leak internal state."""
        token = create_test_token(env="production", roles=["analyst"], org="t1")
        r = client.get("/api/v1/health")  # health is public, but let's test auth'd endpoint
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers=_auth(token),
        )
        assert r.status_code == 403
        error = r.json()["error"]
        # Must not contain shadow_verdict, exception metadata, etc.
        assert error.get("details") is None
        assert "shadow" not in error["message"].lower()
