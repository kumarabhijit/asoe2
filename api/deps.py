"""FastAPI dependency injection for auth, RBAC, tenant, and environment.

Implements architecture_v3.md:
  §11.1 — Authentication (JWT with exp, access vs refresh token types)
  §11.2 — RBAC (5 roles, {resource}:{action} permissions)
  §11.3 — Multi-Tenancy (JWT org claim, partner-role scoping)
  §11.4 — trace_id propagation (X-Trace-ID header)
  §11.5 — Secret management (ASOE_JWT_SECRET env var)
  §11.6 — Environment isolation (JWT env claim vs ASOE_ENV)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import Depends, Header, Request
from pydantic import BaseModel

from api.errors import ASOEError

logger = logging.getLogger("asoe.api.auth")

# ---------------------------------------------------------------------------
# Secret + configuration (architecture_v3.md §11.5)
# ---------------------------------------------------------------------------

def _get_jwt_secret() -> str:
    """Load JWT signing secret from env var, with dev fallback."""
    return os.getenv("ASOE_JWT_SECRET", "asoe-dev-secret-do-not-use-in-production")

_ALGORITHM = "HS256"

# Token lifetimes (architecture_v3.md §11.1)
ACCESS_TOKEN_EXPIRE_SECONDS = 15 * 60       # 15 minutes
REFRESH_TOKEN_EXPIRE_SECONDS = 7 * 24 * 3600  # 7 days


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
    payload = json.loads(_b64url_decode(body_b64))

    # Validate expiry if present
    exp = payload.get("exp")
    if exp is not None and time.time() > exp:
        raise ValueError("Token has expired")

    return payload


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
    retailer_id: Optional[str] = None  # partner-role: scoped to own orders


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
# Environment isolation (architecture_v3.md §11.6)
# ---------------------------------------------------------------------------

def _validate_environment(token_env: str) -> None:
    """Validate JWT env claim matches ASOE_ENV.

    A sandbox token presented to a production service returns 403
    immediately, before any business logic executes.
    """
    server_env = os.getenv("ASOE_ENV", "sandbox")
    if token_env != server_env:
        logger.warning(
            "Environment mismatch: token_env=%s server_env=%s", token_env, server_env
        )
        # Per §11.6: generic 403 with no internal state in details
        raise ASOEError(
            code="ENV_MISMATCH",
            message="Access denied.",
            status_code=403,
        )


# ---------------------------------------------------------------------------
# JWT extraction dependency
# ---------------------------------------------------------------------------

async def get_current_user(
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> AuthenticatedUser:
    """Extract and validate JWT Bearer token from Authorization header.

    Validates: signature, expiry, and environment claim.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise ASOEError(
            code="UNAUTHORIZED",
            message="Missing or invalid Authorization header.",
            status_code=401,
        )

    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = _jwt_decode(token, _get_jwt_secret())
    except ValueError as exc:
        logger.warning("JWT validation failed: %s", exc)
        raise ASOEError(
            code="UNAUTHORIZED",
            message="Invalid or expired token.",
            status_code=401,
        )

    # Environment isolation check (§11.6)
    token_env = payload.get("env", "sandbox")
    _validate_environment(token_env)

    roles = payload.get("roles", [])
    return AuthenticatedUser(
        sub=payload.get("sub", ""),
        email=payload.get("email", ""),
        name=payload.get("name", ""),
        roles=roles,
        org=payload.get("org", ""),
        permissions=_expand_permissions(roles),
        env=token_env,
        retailer_id=payload.get("retailer_id"),
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
    the specified roles."""

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
# WebSocket token validation (public API for ws.py)
# ---------------------------------------------------------------------------

def validate_ws_token(token: str) -> Dict[str, Any]:
    """Validate a JWT token and return its payload.

    Public entry point for WebSocket authentication — avoids importing
    private helpers (_jwt_decode, _get_jwt_secret) across module boundaries.
    """
    return _jwt_decode(token, _get_jwt_secret())


# ---------------------------------------------------------------------------
# Token creation helpers
# ---------------------------------------------------------------------------

def _create_token(
    token_type: str,
    expire_seconds: int,
    sub: str,
    email: str,
    name: str,
    roles: List[str],
    org: str,
    env: str = "sandbox",
    auth_method: Optional[str] = None,
    retailer_id: Optional[str] = None,
) -> str:
    """Create a signed JWT with the given type and expiry."""
    now = int(time.time())
    payload: Dict[str, Any] = {
        "sub": sub,
        "email": email,
        "name": name,
        "roles": roles,
        "org": org,
        "env": env,
        "iat": now,
        "exp": now + expire_seconds,
        "token_type": token_type,
    }
    if token_type == "access":
        payload["permissions"] = _expand_permissions(roles)
    if auth_method:
        payload["auth_method"] = auth_method
    if retailer_id:
        payload["retailer_id"] = retailer_id
    return _jwt_encode(payload, _get_jwt_secret())


def create_access_token(
    sub: str,
    email: str,
    name: str,
    roles: List[str],
    org: str,
    env: str = "sandbox",
    auth_method: Optional[str] = None,
    retailer_id: Optional[str] = None,
) -> str:
    """Create a signed access token (15-minute expiry)."""
    return _create_token(
        "access", ACCESS_TOKEN_EXPIRE_SECONDS,
        sub=sub, email=email, name=name, roles=roles, org=org,
        env=env, auth_method=auth_method, retailer_id=retailer_id,
    )


def create_refresh_token(
    sub: str,
    email: str,
    name: str,
    roles: List[str],
    org: str,
    env: str = "sandbox",
    retailer_id: Optional[str] = None,
) -> str:
    """Create a signed refresh token (7-day expiry)."""
    return _create_token(
        "refresh", REFRESH_TOKEN_EXPIRE_SECONDS,
        sub=sub, email=email, name=name, roles=roles, org=org,
        env=env, retailer_id=retailer_id,
    )


def create_test_token(
    sub: str = "test-user",
    email: str = "test@example.com",
    name: str = "Test User",
    roles: Optional[List[str]] = None,
    org: str = "test-tenant",
    env: str = "sandbox",
    **extra_claims,
) -> str:
    """Create a signed JWT for testing (long expiry, matches ASOE_ENV default)."""
    now = int(time.time())
    payload = {
        "sub": sub,
        "email": email,
        "name": name,
        "roles": roles or ["analyst"],
        "org": org,
        "env": env,
        "iat": now,
        "exp": now + ACCESS_TOKEN_EXPIRE_SECONDS,
        "token_type": "access",
        **extra_claims,
    }
    return _jwt_encode(payload, _get_jwt_secret())
