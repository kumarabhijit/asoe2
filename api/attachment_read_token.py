"""Short-TTL, scoped capability tokens for attachment byte reads (ADR-044 §2.2).

The in-DB / filesystem attachment backends can't mint native object-store signed
URLs, so a scoped read is served through an RBAC-checked mint endpoint + a
token-validated streaming endpoint. The token IS the capability: an HMAC-SHA256
signature binds it to exactly one ``(tenant_id, case_id, attachment_id)`` tuple
plus an expiry, so it is unusable after expiry and cannot be repurposed to read
another tenant's or case's bytes. No PII is in the token (ids + expiry only).

This is access-scoping, not data-governance: retention/TTL of the *bytes* and
encryption-at-rest are out of scope (governance), deferred per ADR-044.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict

from api.deps import _get_jwt_secret
from contracts.policy import ATTACHMENT_READ_URL_TTL_SECONDS


class ReadTokenError(Exception):
    """Raised when a read token is malformed, tampered, or expired."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(payload_b64: str) -> str:
    sig = hmac.new(
        _get_jwt_secret().encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256,
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
    return f"{payload_b64}.{_sign(payload_b64)}"


def verify_read_token(token: str) -> Dict[str, Any]:
    """Validate signature + expiry; return the claims dict. Raises
    ``ReadTokenError`` on any tamper/expiry — the caller maps that to 403."""
    try:
        payload_b64, sig = token.split(".", 1)
    except ValueError:
        raise ReadTokenError("malformed token")
    if not hmac.compare_digest(sig, _sign(payload_b64)):
        raise ReadTokenError("bad signature")
    try:
        claims = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        raise ReadTokenError("malformed payload")
    if not isinstance(claims, dict) or int(claims.get("exp", 0)) < int(time.time()):
        raise ReadTokenError("expired")
    return claims
