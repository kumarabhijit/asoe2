"""Short-TTL, scoped capability tokens for attachment byte reads (ADR-044 §2.2).

The in-DB / filesystem attachment backends can't mint native object-store signed
URLs, so a scoped read is served through an RBAC-checked mint endpoint + a
token-validated streaming endpoint. The token IS the capability: an HMAC-SHA256
signature binds it to exactly one ``(tenant_id, case_id, attachment_id)`` tuple
plus an expiry, so it is unusable after expiry and cannot be repurposed to read
another tenant's or case's bytes. No PII is in the token (ids + expiry only).

This is access-scoping, not data-governance: retention/TTL of the *bytes* and
encryption-at-rest are out of scope (governance), deferred per ADR-044.

PARITY-4 — signing-key separation. The signing key is read from
``ASOE_ATTACHMENT_SIGNING_KEY`` (independently rotatable from
``ASOE_JWT_SECRET``). For backwards compatibility on deploys that
haven't set the new var yet, we fall back to ``ASOE_JWT_SECRET`` —
the next ``set-secrets.sh`` invocation in
``docs/ops/secrets-rotation.md`` sets both keys, after which the JWT
secret stops being read by this module.

Key rotation is supported via ``ASOE_ATTACHMENT_SIGNING_KEY_SECONDARY``:
during an overlap window the secondary key still verifies tokens
minted just before the rotation, but new mints use the primary only.

Future work: migrate to ES256/EdDSA so rotation is transparent (a
single key registry serves both ends without an overlap window).
Tracked in ``docs/plans/ga-preconditions.md`` (created at GA-readiness).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, List, Tuple

from contracts.policy import ATTACHMENT_READ_URL_TTL_SECONDS


class ReadTokenError(Exception):
    """Raised when a read token is malformed, tampered, or expired."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _resolve_signing_keys() -> Tuple[str, List[str]]:
    """Return ``(primary, [secondaries])``.

    Primary is the active mint+verify key. Secondaries are
    verify-only — used during rotation overlap so a token minted
    moments before the rotation still verifies until it expires.

    Resolution order for primary:
      1. ``ASOE_ATTACHMENT_SIGNING_KEY`` (preferred, post-PARITY-4).
      2. ``ASOE_JWT_SECRET`` (legacy fallback for unmigrated deploys).
    """
    primary = (os.getenv("ASOE_ATTACHMENT_SIGNING_KEY") or "").strip()
    if not primary:
        # Legacy fallback. Read ASOE_JWT_SECRET via api.deps so the
        # dev-default sentinel ("asoe-dev-secret-do-not-use-in-production")
        # is consistent across modules.
        from api.deps import _get_jwt_secret
        primary = _get_jwt_secret()
    secondaries_raw = (
        os.getenv("ASOE_ATTACHMENT_SIGNING_KEY_SECONDARY") or ""
    ).strip()
    secondaries = [s.strip() for s in secondaries_raw.split(",") if s.strip()]
    return primary, secondaries


def _sign_with(payload_b64: str, key: str) -> str:
    sig = hmac.new(
        key.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256,
    ).digest()
    return _b64url_encode(sig)


def mint_read_token(
    *, tenant_id: str, case_id: str, attachment_id: str, ttl_seconds: int | None = None,
) -> str:
    """Mint a signed, expiring read token bound to one attachment tuple."""
    ttl = ATTACHMENT_READ_URL_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    claims = {
        "t": tenant_id,
        "c": case_id,
        "a": attachment_id,
        "exp": int(time.time()) + int(ttl),
    }
    payload_b64 = _b64url_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    primary, _ = _resolve_signing_keys()
    return f"{payload_b64}.{_sign_with(payload_b64, primary)}"


def verify_read_token(token: str) -> Dict[str, Any]:
    """Validate signature + expiry; return the claims dict. Raises
    ``ReadTokenError`` on any tamper/expiry — the caller maps that to 403.

    Accepts a signature produced by EITHER the primary key OR any
    secondary key listed in ``ASOE_ATTACHMENT_SIGNING_KEY_SECONDARY``
    (comma-separated). Operators move the old key to secondary during
    a rotation overlap; once the window closes they unset the
    secondary and the old key stops verifying.
    """
    try:
        payload_b64, sig = token.split(".", 1)
    except ValueError:
        raise ReadTokenError("malformed token")
    primary, secondaries = _resolve_signing_keys()
    candidates = [primary, *secondaries]
    if not any(hmac.compare_digest(sig, _sign_with(payload_b64, k)) for k in candidates):
        raise ReadTokenError("bad signature")
    try:
        claims = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        raise ReadTokenError("malformed payload")
    if not isinstance(claims, dict) or int(claims.get("exp", 0)) < int(time.time()):
        raise ReadTokenError("expired")
    return claims
