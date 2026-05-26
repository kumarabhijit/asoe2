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


# ---------------------------------------------------------------------------
# PARITY-3b — Entra ID (Azure AD) JWKS token validation
# ---------------------------------------------------------------------------
#
# When ``ASOE_AUTH_MODE=entra``, ``get_current_user`` first tries to
# validate the incoming bearer token as an RS256 token issued by Entra
# (JWKS-validated, aud/iss/kid pinned). If that fails, it falls back
# to the HS256 seed-token path so a single replica can run mixed
# traffic during a rollout. The seed-only path (``ASOE_AUTH_MODE=seed``,
# the default) skips the Entra branch entirely.

def _entra_enabled() -> bool:
    return os.getenv("ASOE_AUTH_MODE", "seed").lower() == "entra"


def _entra_issuer() -> str:
    return os.getenv("ASOE_ISSUER_URL", "").rstrip("/")


def _entra_audience() -> str:
    return os.getenv("ASOE_CLIENT_ID", "")


def _verify_rs256_signature(token: str, jwk: Dict[str, Any]) -> bool:
    """Verify an RS256 JWT signature against an Entra JWK.

    Isolated so tests can monkeypatch the signature check while still
    exercising the claim-pinning code paths. Production callers route
    through ``_entra_decode`` which calls this on the canonical
    ``signing_input``.
    """
    try:
        import jwt as pyjwt
        from jwt.algorithms import RSAAlgorithm
    except ImportError:
        # PyJWT is in the runtime requirements; surface a clear error
        # rather than crashing the worker on a generic ImportError.
        raise ASOEError(
            code="JWKS_UNAVAILABLE",
            message="JWT validator not installed.",
            status_code=500,
        )
    try:
        public_key = RSAAlgorithm.from_jwk(json.dumps(jwk))
        # PyJWT does its own claim checks; we already pin aud/iss/exp
        # explicitly in _entra_decode, so we let PyJWT do the signature
        # check only by passing options={"verify_aud": False}. The
        # outer caller then checks aud + iss + tid.
        pyjwt.decode(
            token,
            key=public_key,
            algorithms=["RS256"],
            options={"verify_aud": False, "verify_iss": False, "verify_exp": False},
        )
        return True
    except Exception:  # noqa: BLE001 — any failure means signature didn't verify
        return False


def _entra_decode(token: str) -> Dict[str, Any]:
    """Validate + decode an Entra-issued RS256 JWT.

    Pins: ``kid`` (must be present in header AND resolve via JWKS),
    ``iss`` (must equal ``ASOE_ISSUER_URL``), ``aud`` (must equal
    ``ASOE_CLIENT_ID``), ``exp`` (must be in the future). Returns the
    decoded payload.

    Raises ``ASOEError`` (401/403) on any pin failure. Fails closed on
    JWKS-fetch timeouts.
    """
    from api import azure_ad_jwks

    issuer_url = _entra_issuer()
    audience = _entra_audience()
    if not issuer_url or not audience:
        raise ASOEError(
            code="ENTRA_NOT_CONFIGURED",
            message="Entra mode requires ASOE_ISSUER_URL and ASOE_CLIENT_ID.",
            status_code=500,
        )

    parts = token.split(".")
    if len(parts) != 3:
        raise ASOEError(code="UNAUTHORIZED", message="Malformed token.", status_code=401)
    header_b64, body_b64, _sig_b64 = parts

    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(body_b64))
    except Exception as exc:
        raise ASOEError(
            code="UNAUTHORIZED",
            message="Token header/payload undecodable.",
            status_code=401,
        ) from exc

    kid = header.get("kid")
    if not kid:
        raise ASOEError(
            code="UNAUTHORIZED",
            message="Token header is missing 'kid' — Entra always sets one.",
            status_code=401,
        )

    # exp / iss / aud pinning. We do these BEFORE the signature check
    # so a wrong-tenant token doesn't even trigger a JWKS round-trip.
    now = time.time()
    exp = payload.get("exp")
    if exp is None or now > float(exp):
        raise ASOEError(code="UNAUTHORIZED", message="Token expired.", status_code=401)

    iss = payload.get("iss", "")
    if iss.rstrip("/") != issuer_url:
        logger.warning("entra iss mismatch token_iss=%s expected=%s", iss, issuer_url)
        raise ASOEError(
            code="FORBIDDEN",
            message="Token issuer does not match configured tenant.",
            status_code=403,
        )

    aud = payload.get("aud")
    aud_ok = aud == audience or (isinstance(aud, list) and audience in aud)
    if not aud_ok:
        logger.warning("entra aud mismatch token_aud=%s expected=%s", aud, audience)
        raise ASOEError(
            code="FORBIDDEN",
            message="Token audience does not match this app.",
            status_code=403,
        )

    jwk = azure_ad_jwks.get_jwk_for_kid(issuer_url, kid)
    if jwk is None:
        raise ASOEError(
            code="UNAUTHORIZED",
            message="Token kid not found in JWKS.",
            status_code=401,
        )
    if not _verify_rs256_signature(token, jwk):
        raise ASOEError(
            code="UNAUTHORIZED",
            message="Token signature invalid.",
            status_code=401,
        )
    return payload


# Token lifetimes (architecture_v3.md §11.1).
#
# Defaults are intentionally environment-aware rather than hardcoded:
#   * sandbox      — 24h access, 30d refresh. Pre-prod demos and
#                    Playwright smoke runs need a session that survives
#                    a coffee break without the operator hitting a
#                    silent 401 on every WebSocket-driven detail
#                    refresh. The previous fixed 15-minute access token
#                    caused ExceptionDetailPanel to render an empty
#                    "Exception not found" state after exactly 15
#                    minutes of inactivity.
#   * production   — 60min access, 7d refresh. Standard short-lived
#                    access tokens with refresh-token rotation;
#                    matches the §11.1 baseline.
#
# Both can be overridden per deployment via env vars without a code
# change:
#   ASOE_ACCESS_TOKEN_TTL_SECONDS  — int, overrides access token lifetime
#   ASOE_REFRESH_TOKEN_TTL_SECONDS — int, overrides refresh token lifetime
#
# Resolved once at module import. Container Apps re-imports on every
# revision restart, so a deploy with an updated env var picks up the
# new value on the next rollout without any further intervention.

def _resolve_token_ttls() -> tuple[int, int]:
    """Return (access_ttl_seconds, refresh_ttl_seconds) for this deployment.

    Pure helper so the resolution logic is unit-testable without
    process-env mutation in the test body. Empty-string env values are
    treated the same as unset — bicep declares the env vars
    unconditionally and passes ``''`` as the default, so an empty
    string must mean "use the env-driven default", not "TTL of zero
    seconds".
    """
    env = os.getenv("ASOE_ENV", "production").lower()
    if env == "sandbox":
        default_access = 24 * 3600          # 24 hours
        default_refresh = 30 * 24 * 3600    # 30 days
    else:
        default_access = 60 * 60            # 60 minutes
        default_refresh = 7 * 24 * 3600     # 7 days

    def _ttl_or_default(env_var: str, default: int) -> int:
        raw = os.getenv(env_var, "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            # Malformed override (non-integer) — fall back to default
            # rather than crash the worker on startup. The deploy script
            # validates ints already; this guards against hand edits.
            return default
        # Negative or zero is meaningless for a TTL; treat as default.
        return value if value > 0 else default

    access = _ttl_or_default("ASOE_ACCESS_TOKEN_TTL_SECONDS", default_access)
    refresh = _ttl_or_default("ASOE_REFRESH_TOKEN_TTL_SECONDS", default_refresh)
    return access, refresh


ACCESS_TOKEN_EXPIRE_SECONDS, REFRESH_TOKEN_EXPIRE_SECONDS = _resolve_token_ttls()


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
    title: str = ""
    avatar_initials: str = ""
    roles: List[str]
    org: str  # tenant_id
    permissions: List[str] = []
    assigned_accounts: List[str] = []  # empty = all customers
    env: str = "sandbox"
    retailer_id: Optional[str] = None  # partner-role: scoped to own orders


# ---------------------------------------------------------------------------
# Role → permission mapping (architecture_v3.md Section 11.2)
# ---------------------------------------------------------------------------

_ROLE_PERMISSIONS = {
    "analyst": [
        "exceptions:read", "exceptions:approve", "exceptions:escalate",
        "dashboard:read",
    ],
    "manager": [
        "exceptions:read", "exceptions:approve", "exceptions:escalate",
        "exceptions:override", "rules:write", "dashboard:read",
    ],
    "admin": [
        "exceptions:read", "exceptions:approve", "exceptions:escalate",
        "exceptions:override", "rules:write", "users:manage", "policy:write",
        "audit:read", "dashboard:read",
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
    server_env = os.getenv("ASOE_ENV", "production")
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
    payload: Optional[Dict[str, Any]] = None

    # PARITY-3b — when Entra mode is on, validate as an RS256 Entra
    # token first. We fall through to the HS256 seed path on
    # *transport* failures (the token isn't an Entra RS256 token at all)
    # but NOT on *policy* failures (403 from a cross-tenant `iss` or a
    # wrong `aud`). Catching all ASOEErrors would let a cross-tenant
    # token attempt re-validation as HS256 — which silently defeats the
    # tenant boundary if the token happens to also be a valid seed JWT.
    if _entra_enabled():
        try:
            entra_payload = _entra_decode(token)
        except ASOEError as exc:
            if exc.status_code == 403:
                # Policy rejection — propagate; do NOT try the HS256 path.
                raise
            entra_payload = None
        if entra_payload is not None:
            # Project Entra claims into the AuthenticatedUser shape.
            # `groups` → ASOE roles via azure_ad_roles.groups_to_roles.
            from api import azure_ad_roles

            groups = entra_payload.get("groups", []) or []
            roles = azure_ad_roles.groups_to_roles(groups)
            # ASOE_ENV check still applies — an Entra-authenticated
            # operator is constrained to the env the deploy is running
            # in. We synthesise an env claim from the active server
            # env so the downstream contract is identical to seed mode.
            server_env = os.getenv("ASOE_ENV", "production")
            return AuthenticatedUser(
                sub=entra_payload.get("sub", ""),
                email=entra_payload.get("email")
                    or entra_payload.get("preferred_username", ""),
                name=entra_payload.get("name", ""),
                title="",
                avatar_initials="",
                roles=roles,
                org=entra_payload.get("tid", ""),  # Entra tenant id
                permissions=_expand_permissions(roles),
                assigned_accounts=[],
                env=server_env,
                retailer_id=None,
            )

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
        title=payload.get("title", ""),
        avatar_initials=payload.get("avatar_initials", ""),
        roles=roles,
        org=payload.get("org", ""),
        permissions=_expand_permissions(roles),
        assigned_accounts=payload.get("assigned_accounts", []),
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


def require_permission(*allowed_permissions: str):
    """Return a FastAPI dependency that checks the user has at least one of
    the specified permissions (expanded from roles)."""

    async def _check_permission(
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        # user.permissions is already expanded from roles in get_current_user
        granted = set(user.permissions)
        if not any(p in granted for p in allowed_permissions):
            raise ASOEError(
                code="FORBIDDEN",
                message=(
                    "Requires one of permissions: "
                    f"{', '.join(allowed_permissions)}."
                ),
                status_code=403,
            )
        return user

    return _check_permission


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
    title: str = "",
    avatar_initials: str = "",
    assigned_accounts: Optional[List[str]] = None,
) -> str:
    """Create a signed JWT with the given type and expiry."""
    import uuid

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
        # PARITY-3b — jti uniquely identifies the token instance for
        # the refresh-token revocation index. Both access and refresh
        # tokens carry one; only refresh tokens are looked up against
        # the revocation store (access tokens are short-lived enough
        # that allow-listing isn't worth the round trip).
        "jti": uuid.uuid4().hex,
    }
    if token_type == "access":
        payload["permissions"] = _expand_permissions(roles)
    if auth_method:
        payload["auth_method"] = auth_method
    if retailer_id:
        payload["retailer_id"] = retailer_id
    if title:
        payload["title"] = title
    if avatar_initials:
        payload["avatar_initials"] = avatar_initials
    if assigned_accounts:
        payload["assigned_accounts"] = assigned_accounts
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
    title: str = "",
    avatar_initials: str = "",
    assigned_accounts: Optional[List[str]] = None,
) -> str:
    """Create a signed access token (15-minute expiry)."""
    return _create_token(
        "access", ACCESS_TOKEN_EXPIRE_SECONDS,
        sub=sub, email=email, name=name, roles=roles, org=org,
        env=env, auth_method=auth_method, retailer_id=retailer_id,
        title=title, avatar_initials=avatar_initials,
        assigned_accounts=assigned_accounts,
    )


def create_refresh_token(
    sub: str,
    email: str,
    name: str,
    roles: List[str],
    org: str,
    env: str = "sandbox",
    retailer_id: Optional[str] = None,
    title: str = "",
    avatar_initials: str = "",
    assigned_accounts: Optional[List[str]] = None,
) -> str:
    """Create a signed refresh token (7-day expiry)."""
    return _create_token(
        "refresh", REFRESH_TOKEN_EXPIRE_SECONDS,
        sub=sub, email=email, name=name, roles=roles, org=org,
        env=env, retailer_id=retailer_id,
        title=title, avatar_initials=avatar_initials,
        assigned_accounts=assigned_accounts,
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
