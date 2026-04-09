"""Auth endpoints (architecture_v3.md Section 8.2, 11.1).

POST /api/auth/login        — Email/password authentication (admin-only, MFA enforced)
POST /api/auth/sso/init     — SSO initiation (stub)
GET  /api/auth/sso/callback — SSO callback (stub)
POST /api/auth/mfa/verify   — MFA verification (stub)
POST /api/auth/refresh      — Token refresh
GET  /api/auth/me           — Current authenticated user profile

V1 implements stub auth with dev-signed JWTs. Production requires
real IdP integration (Okta, Azure AD) and Key Vault-managed signing keys.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import AuthenticatedUser, create_test_token, get_current_user
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

    # Stub MFA challenge — always require MFA per architecture_v3.md Section 11.1
    mfa_token = create_test_token(
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
    # V1 stub: returns a placeholder redirect URL.
    # Production: constructs SAML AuthnRequest or OIDC authorization URL.
    return {"redirect_url": "https://idp.example.com/auth?client_id=asoe"}


# ---------------------------------------------------------------------------
# GET /api/auth/sso/callback — SSO callback (stub)
# ---------------------------------------------------------------------------

@router.get("/sso/callback")
async def sso_callback() -> dict:
    # V1 stub: returns a test token.
    # Production: validates SAML assertion / OIDC token, issues JWT.
    token = create_test_token(
        sub="sso-user",
        email="sso@example.com",
        name="SSO User",
        roles=["analyst"],
        org="sso-tenant",
    )
    return {
        "access_token": token,
        "refresh_token": token,
        "token_type": "bearer",
    }


# ---------------------------------------------------------------------------
# POST /api/auth/mfa/verify — MFA verification (stub)
# ---------------------------------------------------------------------------

@router.post("/mfa/verify", response_model=AuthTokenResponse)
async def mfa_verify(req: MFAVerifyRequest) -> AuthTokenResponse:
    # V1 stub: accepts any code, issues tokens.
    # Production: validates TOTP code against user's MFA secret.
    if not req.code:
        raise ASOEError(
            code="INVALID_MFA",
            message="MFA code is required.",
            status_code=400,
        )

    token = create_test_token(
        sub="admin-user",
        email="admin@example.com",
        name="Admin User",
        roles=["admin"],
        org="default-tenant",
    )
    return AuthTokenResponse(
        access_token=token,
        refresh_token=token,
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
# POST /api/auth/refresh — Token refresh
# ---------------------------------------------------------------------------

@router.post("/refresh", response_model=AuthTokenResponse)
async def refresh(req: RefreshRequest) -> AuthTokenResponse:
    # V1 stub: issues a new token from the refresh token claims.
    # Production: validates refresh token, rotates it, issues new access token.
    from api.deps import _STUB_SECRET, _jwt_decode

    try:
        payload = _jwt_decode(req.refresh_token, _STUB_SECRET)
    except (ValueError, KeyError):
        raise ASOEError(
            code="INVALID_TOKEN",
            message="Invalid refresh token.",
            status_code=401,
        )

    new_token = create_test_token(
        sub=payload.get("sub", ""),
        email=payload.get("email", ""),
        name=payload.get("name", ""),
        roles=payload.get("roles", []),
        org=payload.get("org", ""),
    )
    return AuthTokenResponse(access_token=new_token)


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
