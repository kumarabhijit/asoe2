# Phase 14 — Auth & Security Hardening

```text
Read architecture_v3.md §11 (Security & Compliance — all subsections),
CLAUDE.md, DESIGN.md §15.2–15.3, and tasks.md (Phase 14).
Implement only Phase 14.

Requirements:

1. Token expiry and types (§11.1):
   - Access tokens: 15-minute expiry, exp + iat claims, token_type: "access"
   - Refresh tokens: 7-day expiry, token_type: "refresh"
   - _jwt_decode() validates exp claim — expired tokens return 401
   - create_access_token() and create_refresh_token() helper functions
   - create_test_token() for tests (uses access token format with standard expiry)

2. Refresh token rotation (§11.1):
   - POST /api/auth/refresh validates token_type == "refresh" — rejects access tokens
   - Issues new access + new refresh token on each call (rotation contract)
   - Preserves all user claims (sub, email, name, roles, org, env, retailer_id)

3. auth_method claim (§11.1):
   - Login → MFA verify flow sets auth_method: "password+mfa"
   - SSO callback sets auth_method: "sso"
   - Claim is present in access tokens for audit differentiation

4. Environment isolation (§11.6):
   - _validate_environment() checks JWT env claim against ASOE_ENV env var
   - Mismatch → ASOEError(code="ENV_MISMATCH", message="Access denied.", status_code=403)
   - Per §11.6: generic 403 with NO details, NO stack traces, NO internal state
   - Called on every authenticated request (inside get_current_user dependency)

5. X-Trace-ID propagation (§11.4):
   - TraceIDMiddleware (api/middleware.py): Starlette BaseHTTPMiddleware
   - Extracts X-Trace-ID from request header or generates UUID
   - Stores in request.state.trace_id
   - Returns X-Trace-ID in every response header (including error responses)
   - Resolve endpoints use request.state.trace_id for graph execution correlation

6. Partner-role scoping (§11.3):
   - AuthenticatedUser includes retailer_id field from JWT claim
   - create_access_token() accepts retailer_id parameter
   - Partner users filtered to own orders (retailer_id match) in list endpoint
   - Partner users blocked from: resolve, override, approve, reject, trace endpoints (RBAC)

7. Configurable JWT secret (§11.5):
   - _get_jwt_secret() reads from ASOE_JWT_SECRET env var
   - Dev fallback used when env var is unset
   - Tokens signed with wrong secret rejected (signature validation in _jwt_decode)

Constraints:
- no changes to the core engine (contracts/, orchestration/, recipes/, etc.)
- all error responses for ENV_MISMATCH must be generic per §11.6
- do not add real IdP integration (Okta, Azure AD) — those are production concerns
- do not add bcrypt or password hashing — login is a stub in V1
- do not add speculative features beyond architecture_v3.md §11

Add tests for: token expiry (exp claim present, expired token rejected, valid accepted),
token types (access vs refresh claims, refresh rejects access token, refresh accepts refresh,
rotation issues both types), auth_method (MFA verify sets "password+mfa", SSO sets "sso"),
trace_id propagation (response header present, client ID echoed, missing generates UUID,
present on error responses), JWT secret (default fallback, custom from env, wrong secret rejected),
environment isolation (matching env accepted, mismatched env rejected with generic 403,
production env rejects sandbox token), partner scoping (retailer_id in token, can list,
cannot resolve/override/trace), error security (no stack traces in error responses,
env mismatch hides details).

Update: DESIGN.md §15.2 (auth docs), AUDITOR_GUIDE.md (§12 API Security Controls),
tasks.md (Phase 14 checklist), .env.example (ASOE_JWT_SECRET, ASOE_ENV).
```
