"""Auth endpoints (architecture_v3.md Section 8.2, 11.1).

POST /api/auth/login        — Email/password authentication (resolves to user store)
POST /api/auth/sso/init     — SSO initiation (stub)
GET  /api/auth/sso/callback — SSO callback (stub)
POST /api/auth/mfa/verify   — MFA verification (stub)
POST /api/auth/refresh      — Token refresh with rotation
GET  /api/auth/me           — Current authenticated user profile

V1 implements stub auth with dev-signed JWTs. Production requires
real IdP integration (Okta, Azure AD) and Key Vault-managed signing keys.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends

from api.deps import (
    AuthenticatedUser,
    _get_jwt_secret,
    _jwt_decode,
    create_access_token,
    create_refresh_token,
    create_test_token,
    get_current_user,
)
from api.errors import ASOEError
from api.schemas import (
    AuthTokenResponse,
    LoginRequest,
    MFAVerifyRequest,
    RefreshRequest,
    UserProfile,
)
from api.users import (
    compute_visible_tabs,
    expand_permissions,
    get_user_by_email,
)

router = APIRouter()


def _current_env() -> str:
    """Return the active ASOE_ENV claim that newly minted tokens must carry.

    Without this, tokens were minted with the keyword default ``"sandbox"``
    regardless of how the container was deployed; the validator at
    ``api.deps._validate_environment`` then rejected every authenticated
    call as ``ENV_MISMATCH`` the moment ops set ``ASOE_ENV=production``.
    """
    return os.getenv("ASOE_ENV", "sandbox").lower()


def _build_user_profile(user_rec) -> UserProfile:
    """Build a UserProfile response from a UserRecord with computed fields."""
    permissions = expand_permissions(user_rec.roles)
    return UserProfile(
        sub=user_rec.sub,
        email=user_rec.email,
        name=user_rec.name,
        title=user_rec.title,
        avatar_initials=user_rec.avatar_initials,
        roles=user_rec.roles,
        org=user_rec.org,
        permissions=permissions,
        assigned_accounts=user_rec.assigned_accounts,
        visible_tabs=compute_visible_tabs(permissions),
    )


def _create_tokens_for_user(user_rec, auth_method: str = "password+mfa"):
    """Issue access + refresh token pair for a user record."""
    env = _current_env()
    access = create_access_token(
        sub=user_rec.sub,
        email=user_rec.email,
        name=user_rec.name,
        roles=user_rec.roles,
        org=user_rec.org,
        env=env,
        auth_method=auth_method,
        title=user_rec.title,
        avatar_initials=user_rec.avatar_initials,
        assigned_accounts=user_rec.assigned_accounts or None,
    )
    refresh = create_refresh_token(
        sub=user_rec.sub,
        email=user_rec.email,
        name=user_rec.name,
        roles=user_rec.roles,
        org=user_rec.org,
        env=env,
        title=user_rec.title,
        avatar_initials=user_rec.avatar_initials,
        assigned_accounts=user_rec.assigned_accounts or None,
    )
    return access, refresh


# ---------------------------------------------------------------------------
# POST /api/auth/login — Email/password (resolves to user store)
# ---------------------------------------------------------------------------

@router.post("/login", response_model=AuthTokenResponse)
async def login(req: LoginRequest) -> AuthTokenResponse:
    if not req.email or not req.password:
        raise ASOEError(
            code="INVALID_CREDENTIALS",
            message="Email and password are required.",
            status_code=400,
        )

    # Look up user from the user store
    user_rec = get_user_by_email(req.email)
    if not user_rec:
        raise ASOEError(
            code="INVALID_CREDENTIALS",
            message="Invalid email or password.",
            status_code=401,
        )

    # V1 stub: accept any non-empty password. Production: validate against IdP.
    access, refresh = _create_tokens_for_user(user_rec, auth_method="password+mfa")
    profile = _build_user_profile(user_rec)

    return AuthTokenResponse(
        access_token=access,
        refresh_token=refresh,
        user=profile,
    )


# ---------------------------------------------------------------------------
# POST /api/auth/sso/init — SSO initiation (stub)
# ---------------------------------------------------------------------------

@router.post("/sso/init")
async def sso_init() -> dict:
    return {"redirect_url": "https://idp.example.com/auth?client_id=asoe"}


# ---------------------------------------------------------------------------
# GET /api/auth/sso/callback — SSO callback (stub)
# ---------------------------------------------------------------------------

@router.get("/sso/callback")
async def sso_callback() -> dict:
    env = _current_env()
    access = create_access_token(
        sub="sso-user",
        email="sso@example.com",
        name="SSO User",
        roles=["analyst"],
        org="sso-tenant",
        env=env,
        auth_method="sso",
    )
    refresh = create_refresh_token(
        sub="sso-user",
        email="sso@example.com",
        name="SSO User",
        roles=["analyst"],
        org="sso-tenant",
        env=env,
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
    }


# ---------------------------------------------------------------------------
# POST /api/auth/mfa/verify — MFA verification (stub)
# ---------------------------------------------------------------------------

@router.post("/mfa/verify", response_model=AuthTokenResponse)
async def mfa_verify(req: MFAVerifyRequest) -> AuthTokenResponse:
    if not req.code:
        raise ASOEError(
            code="INVALID_MFA",
            message="MFA code is required.",
            status_code=400,
        )

    # Default to admin user for backward compat with existing tests
    user_rec = get_user_by_email("marcus.webb@acme-corp.com")
    if not user_rec:
        raise ASOEError(code="INTERNAL", message="Default user not found.", status_code=500)

    access, refresh = _create_tokens_for_user(user_rec, auth_method="password+mfa")
    profile = _build_user_profile(user_rec)

    return AuthTokenResponse(
        access_token=access,
        refresh_token=refresh,
        user=profile,
    )


# ---------------------------------------------------------------------------
# POST /api/auth/refresh — Token refresh with rotation (§11.1)
# ---------------------------------------------------------------------------

@router.post("/refresh", response_model=AuthTokenResponse)
async def refresh(req: RefreshRequest) -> AuthTokenResponse:
    try:
        payload = _jwt_decode(req.refresh_token, _get_jwt_secret())
    except ValueError:
        raise ASOEError(
            code="INVALID_TOKEN",
            message="Invalid or expired refresh token.",
            status_code=401,
        )

    if payload.get("token_type") != "refresh":
        raise ASOEError(
            code="INVALID_TOKEN",
            message="Expected a refresh token.",
            status_code=401,
        )

    # Re-issue against the *active* env, not the env the original token
    # carried. If ops promoted the deployment from sandbox to production
    # mid-session the user must re-authenticate; defaulting to a stale
    # claim would defeat the validator at ``api.deps._validate_environment``.
    fresh_env = _current_env()
    new_access = create_access_token(
        sub=payload.get("sub", ""),
        email=payload.get("email", ""),
        name=payload.get("name", ""),
        roles=payload.get("roles", []),
        org=payload.get("org", ""),
        env=fresh_env,
        retailer_id=payload.get("retailer_id"),
        title=payload.get("title", ""),
        avatar_initials=payload.get("avatar_initials", ""),
        assigned_accounts=payload.get("assigned_accounts") or None,
    )
    new_refresh = create_refresh_token(
        sub=payload.get("sub", ""),
        email=payload.get("email", ""),
        name=payload.get("name", ""),
        roles=payload.get("roles", []),
        org=payload.get("org", ""),
        env=fresh_env,
        retailer_id=payload.get("retailer_id"),
        title=payload.get("title", ""),
        avatar_initials=payload.get("avatar_initials", ""),
        assigned_accounts=payload.get("assigned_accounts") or None,
    )
    return AuthTokenResponse(
        access_token=new_access,
        refresh_token=new_refresh,
    )


# ---------------------------------------------------------------------------
# GET /api/auth/me — Current authenticated user profile
# ---------------------------------------------------------------------------

@router.get("/me", response_model=UserProfile)
async def me(
    user: AuthenticatedUser = Depends(get_current_user),
) -> UserProfile:
    return UserProfile(
        sub=user.sub,
        email=user.email,
        name=user.name,
        title=user.title or None,
        avatar_initials=user.avatar_initials or None,
        roles=user.roles,
        org=user.org,
        permissions=user.permissions,
        assigned_accounts=user.assigned_accounts,
        visible_tabs=compute_visible_tabs(user.permissions),
    )
