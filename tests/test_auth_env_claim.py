"""Tests that newly minted JWTs carry the active ASOE_ENV claim.

Regression: ``_create_tokens_for_user`` and ``sso_callback`` previously
relied on the keyword default ``env="sandbox"`` of
``api.deps.create_access_token``. When ops deployed with
``ASOE_ENV=production`` every login minted a stale ``env=sandbox`` claim,
and the ``api.deps._validate_environment`` check on every authenticated
request rejected the token as ``ENV_MISMATCH``.

The fix in ``api/routes/auth.py`` reads ``ASOE_ENV`` at issuance time and
threads it through ``create_access_token`` / ``create_refresh_token`` for
the login, SSO-callback, MFA-verify, and refresh-rotation paths.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


def _decode_payload(token: str) -> dict:
    """Decode the JWT payload without verifying — tests only want the claims."""
    import base64

    parts = token.split(".")
    pad = "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(parts[1] + pad))


@pytest.fixture(autouse=True)
def _quiet_lifespan(monkeypatch):
    # Ensure the LangGraph orchestration import side effects don't hit
    # external deps during this focused test module.
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "fallback")


def _client(monkeypatch, env: str) -> TestClient:
    monkeypatch.setenv("ASOE_ENV", env)
    monkeypatch.setenv("ASOE_JWT_SECRET", "test-secret-do-not-use")
    import api.app as app_mod
    import importlib

    importlib.reload(app_mod)
    return TestClient(app_mod.create_app())


def test_login_mints_token_with_active_env_when_deployed_non_sandbox(monkeypatch):
    # PARITY-0 Phase 0b: `ASOE_ENV=production` now fail-louds at app boot
    # until real connectors are wired. The env-claim contract being tested
    # here (tokens carry the *active* env, not a stale claim) is the same
    # under any non-sandbox env — use preprod as the representative case.
    client = _client(monkeypatch, "preprod")
    res = client.post(
        "/api/auth/login",
        json={"email": "marcus.webb@acme-corp.com", "password": "anything"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    access_payload = _decode_payload(body["access_token"])
    refresh_payload = _decode_payload(body["refresh_token"])
    assert access_payload["env"] == "preprod"
    assert refresh_payload["env"] == "preprod"


def test_login_mints_token_with_sandbox_env_when_deployed_in_sandbox(monkeypatch):
    client = _client(monkeypatch, "sandbox")
    res = client.post(
        "/api/auth/login",
        json={"email": "marcus.webb@acme-corp.com", "password": "anything"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    access_payload = _decode_payload(body["access_token"])
    assert access_payload["env"] == "sandbox"


def test_sso_callback_mints_with_active_env(monkeypatch):
    # See note in test_login_mints_token_with_active_env_... — preprod is
    # the representative non-sandbox env after Phase 0b.
    client = _client(monkeypatch, "preprod")
    res = client.get("/api/auth/sso/callback")
    assert res.status_code == 200, res.text
    body = res.json()
    access_payload = _decode_payload(body["access_token"])
    refresh_payload = _decode_payload(body["refresh_token"])
    assert access_payload["env"] == "preprod"
    assert refresh_payload["env"] == "preprod"


def test_refresh_reissues_against_active_env_not_token_env(monkeypatch):
    """If the deployment was promoted from sandbox to a non-sandbox env
    mid-session, the refresh path must re-issue with the *current* env. A
    stale token that still says ``env=sandbox`` would otherwise validate
    the rotation request and then mint a token that the very next call
    rejects. (Production fail-louds at boot per PARITY-0 Phase 0b, so the
    promotion target tested here is `preprod` — the env-claim semantics
    are identical.)"""
    # Step 1: login under sandbox.
    client_sbx = _client(monkeypatch, "sandbox")
    res = client_sbx.post(
        "/api/auth/login",
        json={"email": "marcus.webb@acme-corp.com", "password": "anything"},
    )
    refresh_token = res.json()["refresh_token"]
    assert _decode_payload(refresh_token)["env"] == "sandbox"

    # Step 2: redeploy as preprod (env flips, secret stays the same).
    client_pre = _client(monkeypatch, "preprod")

    # Step 3: rotate. The validator decodes the old refresh token (signature
    # OK because secret unchanged) but the *new* tokens it issues must
    # carry env=preprod, not the env from the original refresh payload.
    res = client_pre.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert res.status_code == 200, res.text
    new_access = res.json()["access_token"]
    assert _decode_payload(new_access)["env"] == "preprod"
