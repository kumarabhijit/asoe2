"""PARITY-4 — separate signing keys for JWT vs attachment capability tokens.

Security review CRITICAL: today both ``api/deps.py`` (JWT auth) and
``api/attachment_read_token.py`` (signed-URL capability) read the same
``ASOE_JWT_SECRET`` env var. Rotating one rotates both, and a leaked
attachment-URL signing key would compromise login tokens. The keys must
be independently rotatable.

Contract:

  * ``ASOE_JWT_SECRET``           — HS256 signing key for auth tokens.
                                     Required.
  * ``ASOE_ATTACHMENT_SIGNING_KEY``— HMAC-SHA256 signing key for
                                     attachment capability tokens.
                                     Falls back to ``ASOE_JWT_SECRET``
                                     when unset so old deploys keep
                                     working until they rotate, but
                                     the next ``set-secrets.sh`` run
                                     sets both.
  * The two keys, when both set to DIFFERENT values, must produce
    DIFFERENT signatures on the same payload.
  * Token rotation: the old attachment key keeps verifying for an
    overlap window (``ASOE_ATTACHMENT_SIGNING_KEY_SECONDARY``) so
    minted-before-rotation tokens still work until they expire.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _quiet_lifespan(monkeypatch):
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "fallback")


class TestSeparateSigningKeys:
    def test_attachment_key_defaults_to_jwt_secret_when_unset(self, monkeypatch):
        """Backwards compat: a deploy that hasn't set the new env var
        keeps minting/verifying tokens under the old shared secret."""
        monkeypatch.setenv("ASOE_JWT_SECRET", "primary-jwt-secret-only")
        monkeypatch.delenv("ASOE_ATTACHMENT_SIGNING_KEY", raising=False)
        monkeypatch.delenv("ASOE_ATTACHMENT_SIGNING_KEY_SECONDARY", raising=False)

        from api.attachment_read_token import (
            _resolve_signing_keys,
            mint_read_token,
            verify_read_token,
        )

        primary, secondaries = _resolve_signing_keys()
        assert primary == "primary-jwt-secret-only"
        assert secondaries == []

        tok = mint_read_token(tenant_id="t-1", case_id="c-1", attachment_id="a-1")
        claims = verify_read_token(tok)
        assert claims["t"] == "t-1"

    def test_attachment_key_independent_of_jwt_secret(self, monkeypatch):
        """The point of the separation: different env vars → different
        signatures. A token minted under the attachment key MUST NOT
        verify under the JWT secret."""
        monkeypatch.setenv("ASOE_JWT_SECRET", "the-jwt-only-key")
        monkeypatch.setenv("ASOE_ATTACHMENT_SIGNING_KEY", "the-attachment-only-key")
        monkeypatch.delenv("ASOE_ATTACHMENT_SIGNING_KEY_SECONDARY", raising=False)

        from api.attachment_read_token import (
            mint_read_token,
            verify_read_token,
            ReadTokenError,
            _resolve_signing_keys,
        )

        primary, _ = _resolve_signing_keys()
        assert primary == "the-attachment-only-key"

        tok = mint_read_token(tenant_id="t-1", case_id="c-1", attachment_id="a-1")
        # Verifies under the right key.
        assert verify_read_token(tok)["t"] == "t-1"

        # Swap to a deployment where the attachment key is rotated to
        # something unrelated — the old token must fail to verify.
        monkeypatch.setenv("ASOE_ATTACHMENT_SIGNING_KEY", "rotated-new-attachment-key")
        with pytest.raises(ReadTokenError):
            verify_read_token(tok)

    def test_secondary_key_supports_rotation_overlap(self, monkeypatch):
        """During key rotation, the old key is moved to
        ``ASOE_ATTACHMENT_SIGNING_KEY_SECONDARY`` for an overlap window
        so tokens minted just before rotation still verify until they
        expire."""
        monkeypatch.setenv("ASOE_JWT_SECRET", "irrelevant-here")
        monkeypatch.setenv("ASOE_ATTACHMENT_SIGNING_KEY", "old-key")
        monkeypatch.delenv("ASOE_ATTACHMENT_SIGNING_KEY_SECONDARY", raising=False)

        from api.attachment_read_token import mint_read_token, verify_read_token

        tok_old = mint_read_token(tenant_id="t-1", case_id="c-1", attachment_id="a-1")

        # Operator rotates the primary; old key moves to secondary.
        monkeypatch.setenv("ASOE_ATTACHMENT_SIGNING_KEY", "new-key")
        monkeypatch.setenv("ASOE_ATTACHMENT_SIGNING_KEY_SECONDARY", "old-key")

        # Token minted under the old key still verifies during overlap.
        assert verify_read_token(tok_old)["t"] == "t-1"

        # A fresh mint uses the new key.
        tok_new = mint_read_token(tenant_id="t-2", case_id="c-2", attachment_id="a-2")
        assert verify_read_token(tok_new)["t"] == "t-2"

        # Once the operator removes the secondary (overlap window
        # closed), the old token no longer verifies.
        monkeypatch.delenv("ASOE_ATTACHMENT_SIGNING_KEY_SECONDARY")
        from api.attachment_read_token import ReadTokenError
        with pytest.raises(ReadTokenError):
            verify_read_token(tok_old)

    def test_jwt_secret_not_affected_by_attachment_key_change(self, monkeypatch):
        """Rotating the attachment signing key must NOT invalidate
        live JWT sessions."""
        monkeypatch.setenv("ASOE_JWT_SECRET", "stable-jwt-secret")
        monkeypatch.setenv("ASOE_ATTACHMENT_SIGNING_KEY", "att-key-v1")

        from api.deps import _get_jwt_secret, create_test_token, _jwt_decode

        before = create_test_token(sub="u-1")

        # Rotate ONLY the attachment key.
        monkeypatch.setenv("ASOE_ATTACHMENT_SIGNING_KEY", "att-key-v2")

        # The JWT secret is unchanged → existing JWT still verifies.
        decoded = _jwt_decode(before, _get_jwt_secret())
        assert decoded["sub"] == "u-1"
