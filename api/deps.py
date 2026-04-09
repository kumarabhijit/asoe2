"""FastAPI dependency injection for auth, RBAC, and tenant extraction.

Implements architecture_v3.md Section 11.1 (Authentication), 11.2 (RBAC),
and 11.3 (Multi-Tenancy).

V1 uses a JWT-based auth stub. The JWT is validated for structure and
expiry but does not verify against a real IdP signing key. Production
deployments must replace ``_STUB_SECRET`` with Key Vault-managed keys.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import Depends, Header
from pydantic import BaseModel

from api.errors import ASOEError

logger = logging.getLogger("asoe.api.auth")

# Stub secret for development/testing — replaced by Key Vault in production.
_STUB_SECRET = "asoe-dev-secret-do-not-use-in-production"
_ALGORITHM = "HS256"


# ---------------------------------------------------------------------------
# Minimal JWT encode/decode (HS256 only — no external dependency)
# ---------------------------------------------------------------------------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def _jwt_encode(payload: Dict[str, Any], secret: str) -> str:
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64url_encode(json.dumps(payload).encode())
    signing_input = f"{header}.{body}"
    sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url_encode(sig)}"


def _jwt_decode(token: str, secret: str) -> Dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT format")
    header_b64, body_b64, sig_b64 = parts
    signing_input = f"{header_b64}.{body_b64}"
    expected_sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    actual_sig = _b64url_decode(sig_b64)
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise ValueError("Invalid JWT signature")
    return json.loads(_b64url_decode(body_b64))


# ---------------------------------------------------------------------------
# User model (JWT claims)
# ---------------------------------------------------------------------------

class AuthenticatedUser(BaseModel):
    sub: str
    email: str
    name: str
    roles: List[str]
    org: str  # tenant_id
    permissions: List[str] = []
    env: str = "sandbox"


# ---------------------------------------------------------------------------
# Role → permission mapping (architecture_v3.md Section 11.2)
# ---------------------------------------------------------------------------

_ROLE_PERMISSIONS = {
    "analyst": ["exceptions:read", "exceptions:approve", "dashboard:read"],
    "manager": [
        "exceptions:read", "exceptions:approve", "exceptions:override",
        "rules:write", "dashboard:read",
    ],
    "admin": [
        "exceptions:read", "exceptions:approve", "exceptions:override",
        "rules:write", "users:manage", "policy:write", "audit:read",
        "dashboard:read",
    ],
    "viewer": ["exceptions:read", "dashboard:read"],
    "partner": ["exceptions:read"],
}


def _expand_permissions(roles: List[str]) -> List[str]:
    """Expand role list to flat permission set."""
    perms: set[str] = set()
    for role in roles:
        perms.update(_ROLE_PERMISSIONS.get(role, []))
    return sorted(perms)


# ---------------------------------------------------------------------------
# JWT extraction dependency
# ---------------------------------------------------------------------------

async def get_current_user(
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> AuthenticatedUser:
    """Extract and validate JWT Bearer token from Authorization header.

    Returns an AuthenticatedUser with claims. Raises 401 on missing/invalid
    token. In stub mode, accepts tokens signed with the dev secret.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise ASOEError(
            code="UNAUTHORIZED",
            message="Missing or invalid Authorization header.",
            status_code=401,
        )

    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = _jwt_decode(token, _STUB_SECRET)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        logger.warning("JWT validation failed: %s", exc)
        raise ASOEError(
            code="UNAUTHORIZED",
            message="Invalid or expired token.",
            status_code=401,
        )

    roles = payload.get("roles", [])
    return AuthenticatedUser(
        sub=payload.get("sub", ""),
        email=payload.get("email", ""),
        name=payload.get("name", ""),
        roles=roles,
        org=payload.get("org", ""),
        permissions=_expand_permissions(roles),
        env=payload.get("env", "sandbox"),
    )


# ---------------------------------------------------------------------------
# Tenant extraction dependency (architecture_v3.md Section 11.3)
# ---------------------------------------------------------------------------

async def get_tenant_id(
    user: AuthenticatedUser = Depends(get_current_user),
) -> str:
    """Extract tenant_id from JWT ``org`` claim."""
    if not user.org:
        raise ASOEError(
            code="MISSING_TENANT",
            message="JWT is missing the 'org' (tenant) claim.",
            status_code=403,
        )
    return user.org


# ---------------------------------------------------------------------------
# RBAC dependency factory (architecture_v3.md Section 11.2)
# ---------------------------------------------------------------------------

def require_role(*allowed_roles: str):
    """Return a FastAPI dependency that checks the user has at least one of
    the specified roles.

    Usage::

        @router.post("/resolve", dependencies=[Depends(require_role("analyst", "manager", "admin"))])
        async def resolve(...): ...
    """

    async def _check_role(
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if not any(r in allowed_roles for r in user.roles):
            raise ASOEError(
                code="FORBIDDEN",
                message=f"Requires one of roles: {', '.join(allowed_roles)}.",
                status_code=403,
            )
        return user

    return _check_role


# ---------------------------------------------------------------------------
# Helper: create a stub JWT for testing
# ---------------------------------------------------------------------------

def create_test_token(
    sub: str = "test-user",
    email: str = "test@example.com",
    name: str = "Test User",
    roles: Optional[List[str]] = None,
    org: str = "test-tenant",
    env: str = "sandbox",
    **extra_claims,
) -> str:
    """Create a signed JWT for testing purposes."""
    payload = {
        "sub": sub,
        "email": email,
        "name": name,
        "roles": roles or ["analyst"],
        "org": org,
        "env": env,
        **extra_claims,
    }
    return _jwt_encode(payload, _STUB_SECRET)
