# Deployment Configuration Matrix

**Purpose:** one source of truth for how the system is configured across
**local**, **Vercel (preview)**, and **Azure (pre-prod)** — so the three
environments can be reconciled at a glance. Pairs with
[`.env.example`](../.env.example) (the annotated full var list) and
[`docs/plans/azure-preprod-parity-plan.md`](plans/azure-preprod-parity-plan.md)
(the phased parity programme).

**Scope note:** this repo (`asoe2`) is the **backend** (FastAPI + the
LangGraph engine). The **Vercel** deployment is the Next.js UI in the
separate `asoe-ui` repo; the column below records only what the backend
needs to know about it (CORS, auth handshake). "Production" is intentionally
**fail-loud / unimplemented** — `api/production_gateways.py` raises
`NotImplementedError` so a half-wired prod can never boot silently.

---

## 1. Behaviour matrix

| Dimension | Local (docker-compose / bare) | Vercel preview (UI) | Azure pre-prod (Container Apps) |
|---|---|---|---|
| `ASOE_ENV` | `sandbox` | n/a (UI only) | `preprod` |
| Datastore | SQLite (in-memory unless `DATABASE_URL` set) | n/a | PostgreSQL Flexible Server (`DATABASE_URL` injected) |
| Redis | in-process counter unless `REDIS_URL` set | n/a | Managed Redis (TLS, port 10000) |
| Gateways | sandbox stubs | n/a | preprod stubs; live connectors swap in per `*_DRIVER` + canary |
| Auth mode | `seed` (HS256) | NextAuth → backend | `seed` or `entra` (Azure AD RS256 + JWKS) |
| LLM provider | `fallback` (deterministic) | n/a | `anthropic` via Azure AI Foundry private endpoint |
| Email classifier (ADR-036) | deterministic shim (router falls closed) | n/a | shim by default; live + shadow when provider set |
| Attachment backend | `memory` | n/a | `db` or `object_store` (Azure Blob) |
| CORS | localhost auto-added (sandbox) | — | explicit `CORS_ALLOWED_ORIGINS` + preview regex |
| Access-token TTL | 24h (sandbox default) | — | 60m (non-sandbox default) |
| Secrets source | `.env` file | Vercel env | Azure Key Vault (CSI) |
| Observability | stdlib logging | — | App Insights + OTel (when conn-string set) |
| Egress policy | open | — | public Anthropic egress **blocked** at `ASOE_ENV=production`; Foundry private endpoint required |

---

## 2. Required vs optional env vars per environment

Legend: **R** = required, **O** = optional, **—** = not applicable / leave unset.
All vars are documented in [`.env.example`](../.env.example).

| Variable | Local | Azure pre-prod | Notes |
|---|---|---|---|
| `ASOE_ENV` | R (`sandbox`) | R (`preprod`) | unknown value → fail-loud at boot |
| `DATABASE_URL` | O | R | unset → in-memory SQLite (dev only) |
| `REDIS_URL` | O | R | unset → in-process counter (dev only) |
| `ASOE_JWT_SECRET` | O (dev fallback) | R | never use the dev fallback off-sandbox |
| `ASOE_ATTACHMENT_SIGNING_KEY` | O | R | distinct from JWT secret; `_SECONDARY` for rotation overlap |
| `CORS_ALLOWED_ORIGINS` / `_REGEX` | — (localhost auto) | R | over-permissive regex fails boot |
| `ASOE_LLM_PROVIDER` | O (`fallback`) | O (`anthropic`) | unset → deterministic everywhere |
| `ANTHROPIC_API_KEY` + `ANTHROPIC_BASE_URL` | O | R if provider=anthropic | base-url must be Foundry in production |
| `ASOE_AUTH_MODE` + Entra vars | — | O (`entra`) | `ASOE_ISSUER_URL`, `ASOE_CLIENT_ID`, `ASOE_AZURE_AD_GROUP_TO_ROLE` |
| `ASOE_ATTACHMENT_BACKEND` | O (`memory`) | R (`db`/`object_store`) | `memory` is dev-only |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | — | O | unset → no telemetry export |
| `ASOE_*_DRIVER` + `ASOE_CANARY_PCT_*` | — | O | default `recorded` / `0.0` → stubs, no real traffic |
| Live-connector creds (`ASOE_GRAPH_*`, `ASOE_SAP_*`, `ASOE_OMS_*`, `AZURE_DI_*`) | — | R *iff* the matching driver is live | constructors fail loud without them |
| `ASOE_EMAIL_SUPERGROUP_SHADOW` | O (`1`) | O (`1`) | keep `1` until ECE-calibrated, then `0` to gate on the live model |
| `ASOE_TEST_POSTGRES_URL` | O (tests) | — | integration tests only; not a runtime var |

---

## 3. Parity guarantees baked into code

These are enforced, not just documented:

- **Graceful LLM degradation.** Every constrained-backend lookup goes through
  `constraints.router.get_constrained_backend`, which falls closed to the
  deterministic backend when no provider/key is configured, and on
  kill-switch / explain-mode / per-task disable. So local, Vercel-preview and
  an unconfigured Azure all behave identically (deterministic) — divergence
  only happens when an operator *opts in* to a live provider.
- **Email classifier (ADR-036 Phase 3)** rides that same router via the
  `email_supergroup` task, and additionally runs the live model in
  shadow/observe until ECE-calibrated — so promoting it is an explicit,
  per-environment env flip, never an accident of deployment.
- **Fail-loud boots.** Unknown `ASOE_ENV`, over-permissive CORS regex, a live
  connector driver without its creds, and `ASOE_ENV=production` all refuse to
  start rather than degrade silently.
- **Drift lock.** `tests/test_env_example_parity.py` asserts every operator-
  facing `ASOE_*` / `CORS_*` / `AZURE_*` env var read in non-test source is
  documented in `.env.example`, so this matrix and the example file cannot
  silently fall behind the code.

---

## 4. What is NOT yet parity-complete

Tracked in `azure-preprod-parity-plan.md`; **none of these are code gaps in
this repo** — they are platform/ops actions:

1. Deploy + smoke against an authenticated Azure tenant (the bicep +
   `scripts/deploy-azure.sh` exist; they just need an auth'd run).
2. The 4 Playwright journeys against the deployed preprod URLs.
3. Live gateway **transports** (Graph / Azure DI / S/4HANA / OMS) land behind
   the nightly `-m live` pytest mark; the live-vs-stub routers + canary
   plumbing are already shipped.
