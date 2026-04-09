"""Auth endpoints (architecture_v3.md Section 8.2, 11.1).

POST /api/auth/login        — Email/password authentication (admin-only, MFA enforced)
POST /api/auth/sso/init     — SSO initiation (stub)
GET  /api/auth/sso/callback — SSO callback (stub)
POST /api/auth/mfa/verify   — MFA verification (stub)
POST /api/auth/refresh      — Token refresh with rotation
GET  /api/auth/me           — Current authenticated user profile

V1 implements stub auth with dev-signed JWTs. Production requires
real IdP integration (Okta, Azure AD) and Key Vault-managed signing keys.
"""

from __future__ import annotations

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

router = APIRouter()


# ---------------------------------------------------------------------------
# POST /api/auth/login — Email/password (admin-only, MFA-enforced)
# ---------------------------------------------------------------------------

@router.post("/login", response_model=AuthTokenResponse)
async def login(req: LoginRequest) -> AuthTokenResponse:
    # V1 stub: accepts any admin credentials, returns MFA challenge.
    # Production: validate against user store, confirm admin role, issue MFA token.
    if not req.email or not req.password:
        raise ASOEError(
            code="INVALID_CREDENTIALS",
            message="Email and password are required.",
            status_code=400,
        )

    # Stub MFA challenge — always require MFA per §11.1
    mfa_token = create_access_token(
        sub="mfa-pending",
        email=req.email,
        name="MFA Pending",
        roles=["admin"],
        org="pending",
    )
    return AuthTokenResponse(
        access_token="",
        mfa_required=True,
        mfa_token=mfa_token,
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

    # §11.1: JWT includes auth_method: "password+mfa" for audit differentiation
    access = create_access_token(
        sub="admin-user",
        email="admin@example.com",
        name="Admin User",
        roles=["admin"],
        org="default-tenant",
        auth_method="password+mfa",
    )
    refresh = create_refresh_token(
        sub="admin-user",
        email="admin@example.com",
        name="Admin User",
        roles=["admin"],
        org="default-tenant",
    )
    return AuthTokenResponse(
        access_token=access,
        refresh_token=refresh,
        user=UserProfile(
            sub="admin-user",
            email="admin@example.com",
            name="Admin User",
            roles=["admin"],
            org="default-tenant",
            permissions=["exceptions:read", "exceptions:approve", "exceptions:override",
                         "rules:write", "users:manage", "policy:write", "audit:read",
                         "dashboard:read"],
        ),
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

    # Verify this is a refresh token, not an access token
    if payload.get("token_type") != "refresh":
        raise ASOEError(
            code="INVALID_TOKEN",
            message="Expected a refresh token.",
            status_code=401,
        )

    # Issue new access token + rotated refresh token
    new_access = create_access_token(
        sub=payload.get("sub", ""),
        email=payload.get("email", ""),
        name=payload.get("name", ""),
        roles=payload.get("roles", []),
        org=payload.get("org", ""),
        env=payload.get("env", "sandbox"),
        retailer_id=payload.get("retailer_id"),
    )
    new_refresh = create_refresh_token(
        sub=payload.get("sub", ""),
        email=payload.get("email", ""),
        name=payload.get("name", ""),
        roles=payload.get("roles", []),
        org=payload.get("org", ""),
        env=payload.get("env", "sandbox"),
        retailer_id=payload.get("retailer_id"),
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
        roles=user.roles,
        org=user.org,
        permissions=user.permissions,
    )
