"""Auth endpoints (architecture_v3.md Section 8.2, 11.1).

POST /api/auth/login        — Email/password authentication (resolves to user store)
POST /api/auth/sso/init     — SSO initiation (stub)
GET  /api/auth/sso/callback — SSO callback (stub)
POST /api/auth/mfa/verify   — MFA verification (stub)
POST /api/auth/refresh      — Token refresh with rotation
GET  /api/auth/me           — Current authenticated user profile
POST /api/auth/switch       — Switch user in sandbox mode (returns new JWT)

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
    UserListResponse,
    UserProfile,
)
from api.users import (
    compute_visible_tabs,
    expand_permissions,
    get_user_by_email,
    get_user_by_sub,
    list_users,
)

router = APIRouter()


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
    access = create_access_token(
        sub=user_rec.sub,
        email=user_rec.email,
        name=user_rec.name,
        roles=user_rec.roles,
        org=user_rec.org,
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
    access = create_access_token(
        sub="sso-user",
        email="sso@example.com",
        name="SSO User",
        roles=["analyst"],
        org="sso-tenant",
        auth_method="sso",
    )
    refresh = create_refresh_token(
        sub="sso-user",
        email="sso@example.com",
        name="SSO User",
        roles=["analyst"],
        org="sso-tenant",
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

    new_access = create_access_token(
        sub=payload.get("sub", ""),
        email=payload.get("email", ""),
        name=payload.get("name", ""),
        roles=payload.get("roles", []),
        org=payload.get("org", ""),
        env=payload.get("env", "sandbox"),
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
        env=payload.get("env", "sandbox"),
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


# ---------------------------------------------------------------------------
# POST /api/auth/switch — Switch user (sandbox mode only)
# ---------------------------------------------------------------------------

@router.post("/switch", response_model=AuthTokenResponse)
async def switch_user(
    req: LoginRequest,
    _user: AuthenticatedUser = Depends(get_current_user),
) -> AuthTokenResponse:
    """Switch to a different user in sandbox mode. Requires valid current JWT.

    Issues a new JWT for the target user — full server round-trip, not
    client-side localStorage manipulation. Blocked in production.
    """
    server_env = os.getenv("ASOE_ENV", "sandbox")
    if server_env == "production":
        raise ASOEError(
            code="FORBIDDEN",
            message="User switching is disabled in production.",
            status_code=403,
        )

    target = get_user_by_email(req.email)
    if not target:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"User '{req.email}' not found.",
            status_code=404,
        )

    access, refresh = _create_tokens_for_user(target, auth_method="switch")
    profile = _build_user_profile(target)

    return AuthTokenResponse(
        access_token=access,
        refresh_token=refresh,
        user=profile,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/users — List available users (sandbox only)
# ---------------------------------------------------------------------------
# Note: mounted at /api/auth prefix, so full path is /api/auth/users

@router.get("/users", response_model=UserListResponse)
async def list_available_users(
    _user: AuthenticatedUser = Depends(get_current_user),
) -> UserListResponse:
    """List all available users for the sandbox user switcher.

    Requires authentication. In production, this would be admin-only.
    """
    server_env = os.getenv("ASOE_ENV", "sandbox")
    if server_env == "production":
        raise ASOEError(
            code="FORBIDDEN",
            message="User listing is disabled in production.",
            status_code=403,
        )

    profiles = [_build_user_profile(u) for u in list_users()]
    return UserListResponse(data=profiles)
