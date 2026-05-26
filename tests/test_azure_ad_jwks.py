"""PARITY-3b — JWKS validation, kid/aud/iss pinning, role mapping.

Failure modes the parity plan calls CRITICAL:

* Cross-tenant `iss` accepted — a token minted in a different Entra
  tenant must 403, not authenticate. Phase 3b ships with
  ``signInAudience: AzureADMyOrg`` (single-tenant per Decision Q1).
* `aud` not pinned — a token issued to a *different* client app in the
  same tenant must not authenticate.
* `kid` ignored — JWKS rotation must trigger a JWKS refresh and a
  fresh validation, not a silent fallback to a cached key.
* JWKS-fetch timeout treated as "trust the token" — must fail closed.

These tests live in test_azure_ad_jwks because the JWKS code path is
gated by ``ASOE_AUTH_MODE=entra``; the existing HS256 seed path is
covered by tests/test_auth_env_claim.py.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

import pytest


pytestmark = pytest.mark.usefixtures("_quiet_lifespan")


@pytest.fixture(autouse=True)
def _quiet_lifespan(monkeypatch):
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "fallback")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_unsigned_jwt(payload: dict, kid: str = "test-kid") -> str:
    """Build a JWT with a real-looking header but a placeholder signature.

    Tests for kid/aud/iss/exp pinning don't need a working RS256
    signature — the validator should reject these tokens BEFORE the
    signature check (or fail the signature check against the test
    JWKS). We supply a deterministic header so the kid pinning logic
    has something to dispatch on.
    """
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    body = _b64url(json.dumps(header).encode()) + "." + _b64url(json.dumps(payload).encode())
    sig = _b64url(b"\x00" * 256)
    return f"{body}.{sig}"


# ---------------------------------------------------------------------------
# JWKS cache + fail-closed behaviour
# ---------------------------------------------------------------------------

class TestJwksCacheBehaviour:
    def test_unknown_kid_triggers_refresh(self, monkeypatch):
        from api import azure_ad_jwks

        # Reset the singleton cache for this test.
        azure_ad_jwks.reset_cache()

        calls = {"n": 0}

        def fake_fetch(issuer_url: str) -> dict[str, Any]:
            calls["n"] += 1
            # First call returns key "kid-a"; second call (post-rotation)
            # returns "kid-b" too. The validator must trigger the second
            # call when it sees an unknown kid.
            if calls["n"] == 1:
                return {"keys": [{"kid": "kid-a", "kty": "RSA", "n": "x", "e": "AQAB"}]}
            return {"keys": [
                {"kid": "kid-a", "kty": "RSA", "n": "x", "e": "AQAB"},
                {"kid": "kid-b", "kty": "RSA", "n": "y", "e": "AQAB"},
            ]}

        monkeypatch.setattr(azure_ad_jwks, "_fetch_jwks_raw", fake_fetch)

        # Prime the cache.
        first = azure_ad_jwks.get_jwk_for_kid("https://example/issuer", "kid-a")
        assert first is not None
        assert calls["n"] == 1

        # Cache hit — no refresh.
        again = azure_ad_jwks.get_jwk_for_kid("https://example/issuer", "kid-a")
        assert again is not None
        assert calls["n"] == 1

        # Unknown kid → refresh, then resolve.
        rotated = azure_ad_jwks.get_jwk_for_kid("https://example/issuer", "kid-b")
        assert rotated is not None
        assert calls["n"] == 2

    def test_fetch_timeout_fails_closed(self, monkeypatch):
        from api import azure_ad_jwks
        from api.errors import ASOEError

        azure_ad_jwks.reset_cache()

        def fake_fetch(issuer_url: str) -> dict[str, Any]:
            raise TimeoutError("JWKS endpoint unreachable")

        monkeypatch.setattr(azure_ad_jwks, "_fetch_jwks_raw", fake_fetch)

        with pytest.raises(ASOEError) as exc:
            azure_ad_jwks.get_jwk_for_kid("https://example/issuer", "kid-x")
        assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# aud / iss / kid / cross-tenant pinning
# ---------------------------------------------------------------------------

class TestEntraTokenValidation:
    """Validation MUST pin aud, iss, kid before accepting the token."""

    @pytest.fixture(autouse=True)
    def _entra_mode(self, monkeypatch):
        monkeypatch.setenv("ASOE_AUTH_MODE", "entra")
        monkeypatch.setenv(
            "ASOE_ISSUER_URL",
            "https://login.microsoftonline.com/tenant-aaa/v2.0",
        )
        monkeypatch.setenv("ASOE_CLIENT_ID", "client-asoe")
        from api import azure_ad_jwks
        azure_ad_jwks.reset_cache()

        def fake_fetch(issuer_url: str) -> dict[str, Any]:
            return {"keys": [{"kid": "test-kid", "kty": "RSA", "n": "x", "e": "AQAB"}]}

        monkeypatch.setattr(azure_ad_jwks, "_fetch_jwks_raw", fake_fetch)

        # Stub the signature verification so we can exercise the
        # claim-pinning code paths without minting real RS256 sigs.
        from api import deps
        monkeypatch.setattr(
            deps,
            "_verify_rs256_signature",
            lambda token, jwk: True,
            raising=False,
        )

    def test_cross_tenant_iss_rejected(self, monkeypatch):
        """Token whose iss is from tenant-bbb (not the configured tenant-aaa)
        must be rejected even if signature, aud, exp are all valid.
        """
        from api.deps import _entra_decode  # noqa: F401 — assert importability
        from api.errors import ASOEError

        # Issuer = tenant-bbb, not the configured tenant-aaa.
        payload = {
            "iss": "https://login.microsoftonline.com/tenant-bbb/v2.0",
            "aud": "client-asoe",
            "sub": "user-1",
            "exp": int(time.time()) + 3600,
            "groups": [],
        }
        token = _make_unsigned_jwt(payload)
        from api.deps import _entra_decode
        with pytest.raises(ASOEError) as exc:
            _entra_decode(token)
        assert exc.value.status_code in (401, 403)

    def test_wrong_aud_rejected(self, monkeypatch):
        from api.deps import _entra_decode
        from api.errors import ASOEError

        payload = {
            "iss": "https://login.microsoftonline.com/tenant-aaa/v2.0",
            "aud": "another-client",
            "sub": "user-1",
            "exp": int(time.time()) + 3600,
            "groups": [],
        }
        token = _make_unsigned_jwt(payload)
        with pytest.raises(ASOEError) as exc:
            _entra_decode(token)
        assert exc.value.status_code in (401, 403)

    def test_missing_kid_rejected(self, monkeypatch):
        from api.deps import _entra_decode
        from api.errors import ASOEError

        # Build a token whose header carries no kid. The validator must
        # not silently dispatch to "the first key in the JWKS".
        payload = {
            "iss": "https://login.microsoftonline.com/tenant-aaa/v2.0",
            "aud": "client-asoe",
            "sub": "user-1",
            "exp": int(time.time()) + 3600,
        }
        header = {"alg": "RS256", "typ": "JWT"}  # no kid
        body = _b64url(json.dumps(header).encode()) + "." + _b64url(json.dumps(payload).encode())
        token = f"{body}.{_b64url(b'x' * 64)}"
        with pytest.raises(ASOEError):
            _entra_decode(token)

    def test_expired_token_rejected(self, monkeypatch):
        from api.deps import _entra_decode
        from api.errors import ASOEError

        payload = {
            "iss": "https://login.microsoftonline.com/tenant-aaa/v2.0",
            "aud": "client-asoe",
            "sub": "user-1",
            "exp": int(time.time()) - 60,  # 1 min ago
            "groups": [],
        }
        token = _make_unsigned_jwt(payload)
        with pytest.raises(ASOEError):
            _entra_decode(token)


class TestEntraPolicyRejectionDoesNotFallThrough:
    """A cross-tenant Entra token MUST NOT fall through to HS256
    validation. Catching all ASOEErrors indiscriminately would let a
    token rejected for a 403-class policy reason silently re-attempt
    as a seed token; if it happens to also be a valid HS256 JWT it
    would succeed.

    Regression for the code-review HIGH finding on
    api/deps.py::get_current_user.
    """

    @pytest.fixture(autouse=True)
    def _entra_mode(self, monkeypatch):
        monkeypatch.setenv("ASOE_AUTH_MODE", "entra")
        monkeypatch.setenv(
            "ASOE_ISSUER_URL",
            "https://login.microsoftonline.com/tenant-aaa/v2.0",
        )
        monkeypatch.setenv("ASOE_CLIENT_ID", "client-asoe")
        monkeypatch.setenv("ASOE_JWT_SECRET", "shared-test-secret")
        from api import azure_ad_jwks
        azure_ad_jwks.reset_cache()

        def fake_fetch(issuer_url):
            return {"keys": [{"kid": "test-kid", "kty": "RSA", "n": "x", "e": "AQAB"}]}

        monkeypatch.setattr(azure_ad_jwks, "_fetch_jwks_raw", fake_fetch)

        from api import deps
        monkeypatch.setattr(deps, "_verify_rs256_signature", lambda t, j: True, raising=False)

    @pytest.mark.asyncio
    async def test_cross_tenant_iss_does_not_fall_through_to_hs256(self, monkeypatch):
        """A cross-tenant Entra token is rejected with 403. If it ALSO
        happened to be a valid HS256 token (worst-case crafted attack),
        the validator must STILL refuse it — not silently authenticate."""
        from api.deps import get_current_user, _jwt_encode, _get_jwt_secret
        from api.errors import ASOEError

        # Build a JWT with: (a) Entra-shaped header WITH `kid` (so
        # _entra_decode picks it up first), (b) a cross-tenant `iss`,
        # (c) a valid HS256 signature so the seed path WOULD accept it
        # if we let it fall through.
        header = {"alg": "RS256", "typ": "JWT", "kid": "test-kid"}
        body = {
            "iss": "https://login.microsoftonline.com/tenant-bbb/v2.0",  # WRONG
            "aud": "client-asoe",
            "sub": "u-1",
            "exp": int(time.time()) + 3600,
            "env": "sandbox",
            "roles": ["admin"],  # crafted: would expand high perms if accepted
            "org": "attacker-tenant",
        }
        h = _b64url(json.dumps(header).encode())
        b = _b64url(json.dumps(body).encode())
        signing_input = f"{h}.{b}"
        import hashlib, hmac
        sig = _b64url(
            hmac.new(
                _get_jwt_secret().encode(),
                signing_input.encode(),
                hashlib.sha256,
            ).digest()
        )
        crafted = f"{signing_input}.{sig}"

        with pytest.raises(ASOEError) as exc:
            await get_current_user(authorization=f"Bearer {crafted}")
        assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Role mapping (Entra group id → ASOE role)
# ---------------------------------------------------------------------------

class TestRoleMapping:
    def test_known_group_maps_to_role(self, monkeypatch):
        from api import azure_ad_roles

        monkeypatch.setenv(
            "ASOE_AZURE_AD_GROUP_TO_ROLE",
            "group-aaa:analyst,group-mmm:manager,group-zzz:admin",
        )
        # Reload the module-level mapping reader.
        roles = azure_ad_roles.groups_to_roles(["group-mmm", "group-zzz"])
        assert "manager" in roles
        assert "admin" in roles

    def test_unknown_group_yields_empty(self, monkeypatch):
        from api import azure_ad_roles
        monkeypatch.setenv(
            "ASOE_AZURE_AD_GROUP_TO_ROLE",
            "group-aaa:analyst",
        )
        roles = azure_ad_roles.groups_to_roles(["group-not-mapped"])
        assert roles == []

    def test_empty_groups_yields_empty(self, monkeypatch):
        from api import azure_ad_roles
        monkeypatch.setenv("ASOE_AZURE_AD_GROUP_TO_ROLE", "group-aaa:analyst")
        assert azure_ad_roles.groups_to_roles([]) == []
        assert azure_ad_roles.groups_to_roles(None) == []  # type: ignore[arg-type]

    def test_roles_claim_shape_is_string_list(self, monkeypatch):
        """Contract lock: the UI continues to read `user.roles` as a
        flat string list. The PARITY-3a frontend test asserts the same
        thing on the UI side."""
        from api import azure_ad_roles
        monkeypatch.setenv(
            "ASOE_AZURE_AD_GROUP_TO_ROLE",
            "group-aaa:analyst,group-mmm:manager",
        )
        roles = azure_ad_roles.groups_to_roles(["group-aaa", "group-mmm"])
        assert isinstance(roles, list)
        assert all(isinstance(r, str) for r in roles)


# ---------------------------------------------------------------------------
# Refresh-token revocation
# ---------------------------------------------------------------------------

class TestRefreshTokenRevocation:
    def test_revoked_token_rejected(self, monkeypatch):
        from api import refresh_token_revocation as rtr
        rtr.reset_store()

        # Issue a fake refresh-token id (jti) and revoke it.
        rtr.revoke("jti-revoked-1", reason="logout")
        assert rtr.is_revoked("jti-revoked-1") is True
        assert rtr.is_revoked("jti-still-active-1") is False

    def test_revocation_is_idempotent(self):
        from api import refresh_token_revocation as rtr
        rtr.reset_store()
        rtr.revoke("jti-dup-1", reason="rotation")
        rtr.revoke("jti-dup-1", reason="rotation")
        assert rtr.is_revoked("jti-dup-1")
