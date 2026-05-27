# Azure Pre-Prod ↔ Vercel Dev Parity Plan

**Status:** Finalized v3 (2026-05-26 — six expert reviews folded in + all nine open questions resolved).
**Owner:** Platform + Backend + Frontend (joint).
**Scope:** Bring Azure pre-prod to feature parity with today's Vercel dev
(mocked-layers) UX, then progressively unlock real-data pre-prod.

### PARITY-0 implementation status (2026-05-26)

| Acceptance criterion | Status |
|---|---|
| Env-driven gateway dispatch (sandbox / preprod / production) with fail-loud on unknown env | **✓ shipped** (api/preprod_gateways.py + api/production_gateways.py + api/app.py::_register_gateways_for_env) |
| CORS regex validation at startup (refuse `.*`, refuse SaaS-wildcard) + INFO log of resolved allowlist | **✓ shipped** (api/app.py::_validate_cors_regex; 18 tests) |
| Postgres `connect_timeout` injected with 60s ceiling | **✓ shipped** (db/connection.py::_ensure_connect_timeout; 9 tests) |
| VNet + private-Postgres + Blob upgrade path commented into bicep | **✓ shipped** (infra/main.bicep — pure comments, no deploy-shape change) |
| Image scan + secret-scan + dep-audit CI gates | **✓ shipped, advisory** (.github/workflows/tests.yml pip-audit + gitleaks; docker-build.yml Trivy). Strict promotion is a follow-up PR once baseline is clean. |
| Non-root container verified in CI | **✓ shipped, strict** (docker-build.yml — fails the build if image runs as root) |
| Manual secrets-rotation runbook | **✓ shipped** (docs/ops/secrets-rotation.md) |
| End-to-end inbox-section coverage under ASOE_ENV=preprod | **✓ shipped** (tests/test_preprod_gateway_registration.py — 9 tests; mirrors the sandbox contract) |
| Deploy + smoke against an Azure tenant | **Pending — platform team action** (the bicep + workflow already exist; just needs an auth'd `scripts/deploy-azure.sh` run) |
| 4 Playwright journeys against the preprod URLs | **Pending — depends on deploy** |
| UI seed user logs in, every section populates against deployed backend | **Pending — depends on deploy** |

**Next action after deploy:** start **PARITY-0.5** (audit-chain
tombstone routing — gates real-tenant data).

### PARITY-0.5 implementation status (2026-05-26)

| Acceptance criterion | Status |
|---|---|
| `erase_attachment` writes the tombstone into `policy_audit_log` BEFORE deleting bytes (proof-of-erasure invariant) | **✓ shipped** (`gateways/attachment_store.py::erase_attachment` — 6 tests) |
| `GET /api/v1/attachments/{id}/erasure-certificate` returns the chain-proof tombstone | **✓ shipped** (`api/routes/attachments.py::attachment_erasure_certificate` — 6 tests; manager+admin RBAC; tenant-scoped) |
| `AttachmentErasureTombstone` schema locked in `compliance/audit_bearing_registry.yaml` with CODEOWNERS gate | **✓ shipped** (9 rows added; .github/CODEOWNERS already gates /compliance/) |
| Audit chain still verifies after multiple erasures | **✓ shipped** (`test_audit_chain_verifies_after_erasure`) |
| `verify_audit_chain` integration | **✓ shipped** (certificate endpoint runs it inline) |
| PII-free tombstone invariant (no `content`, no `name`) | **✓ shipped** (registry header + adapter logic + regression test) |
| Real-tenant data gate cleared | **✓ Phase 0.5 acceptance criteria met** — preprod deploy can now safely accept real-tenant data (with the rest of Phase 1-5 still required for full real-data parity)

### PARITY-3a/3b/4/5/6/7/8 implementation status (2026-05-26)

| Phase | Status |
|---|---|
| **PARITY-3a** Frontend NextAuth dual-provider scaffold | **✓ shipped** (`asoe-ui/src/lib/auth.ts`, `src/auth/azure-ad.ts`, `src/components/ui/PreprodIdentityBanner.tsx`, `src/middleware.ts` three-branch, `/403` page, `docs/testing/auth-modes.md`) |
| **PARITY-3b** Backend Entra JWKS + role mapping | **✓ shipped** (`api/azure_ad_jwks.py` 1h TTL cache + kid-rotation refresh + fail-closed, `api/azure_ad_roles.py` group→role, `api/refresh_token_revocation.py` jti index, `api/deps.py::_entra_decode` with aud/iss/kid/exp pinning, cross-tenant 403 regression test, refresh rotation revokes old jti) |
| **PARITY-4** Key Vault + separate signing keys | **✓ shipped** (`infra/main.bicep` Key Vault RBAC + 90d soft-delete + purge-protection; `api/attachment_read_token.py` reads ASOE_ATTACHMENT_SIGNING_KEY with SECONDARY rotation slot; `docs/ops/secrets-rotation.md` extended with overlap + break-glass procedures) |
| **PARITY-5** App Insights + OTel + @audit_bearing | **✓ shipped** (`infra/main.bicep` dedicated workspace + 90d/30d retention; `api/observability/otel.py` opt-in init; `api/observability/audit_bearing.py` decorator + multi-line lint; `compliance/audit_bearing_exemptions.yaml` grandfather list; `api/observability/alerts.py` six pre-defined alerts; `api/observability/log_redaction.py` PII scrubber) |
| **PARITY-6** Real-connector scaffolding | **✓ scaffolding shipped** (`contracts/policy.py::GATEWAY_TIMEOUT_S` per-gateway budgets wired into `gateways/executor.py`; `api/dead_letter_queue.py` tenant-scoped DLQ; `gateways/azure_di_egress_redaction.py` Luhn-checked CC + SSN + IBAN scrubber; `tests/eval/shadow_mode_thresholds.yaml` Q9 thresholds; `docs/ops/fixture-capture.md` cadence + sanitisation checklist). Real per-connector live wiring is per-sub-phase. |
| **PARITY-6.1** Microsoft Graph live `email_intake` | **✓ live-or-stub router shipped** (`gateways/msgraph_intake.py::GraphIntakeGateway` + `LiveGraphIntakeBackend` behind `ASOE_EMAIL_INTAKE_DRIVER=graph`; ShadowRunner-gated; `ASOE_CANARY_PCT_EMAIL_INTAKE` percentage rollout; terminal failure → DLQ + stub fallback; `fetch_message` body egress through `redact_for_azure_di`; `api/preprod_gateways.py` swaps the stub when the driver env is set; per-connector CHANGELOG at `gateways/changelog/email_intake.md`). Live transport (`LiveGraphIntakeBackend.execute`) lands behind nightly `-m live` mark. |
| **PARITY-6.2** AzureDI live `document_extraction` | **✓ live backend shipped** (`gateways/document_extraction.py::LiveDocumentIntelligenceBackend` + `resolve_backend()` selector behind `ASOE_DOCUMENT_EXTRACTION_BACKEND=live`; constructor refuses without endpoint+key/MI to fail loud; `extract_anchors` now calls `api.metrics.record_extraction_drift` on every result so the `extraction-drift` alert has data; CHANGELOG at `gateways/changelog/document_extraction.md`). Live `propose()` HTTP transport lands behind nightly `-m live` mark. |
| **PARITY-6.3** S/4HANA live SAP domains | **✓ live-or-stub router shipped for all seven domains** (`gateways/sap_live.py::SapDomainGateway` + shared `LiveSapBackend` behind `ASOE_SAP_DRIVER=s4hana`; ShadowRunner-gated; `ASOE_CANARY_PCT_SAP` shared rollout %; terminal failure → DLQ under `source="sap"` + stub fallback; `api/preprod_gateways.py` swaps the seven domains when the driver env is set; per-connector CHANGELOG at `gateways/changelog/sap.md`). Domains: `sap_order`, `sap_doc`, `sap_contract`, `promotion`, `sap_block`, `sap_customer_master`, `sla_contract`. Live transport raises `NotImplementedError` until nightly `-m live` mark lands (Decision Q3 — real S/4HANA preprod creds preferred, SAP Cloud trial fallback). |
| **PARITY-6.4** OMS live connector | **✓ live-or-stub router shipped** (`gateways/oms_live.py::OmsGateway` + `LiveOmsBackend` behind `ASOE_OMS_DRIVER=live`; same shadow + canary + DLQ pattern; `ASOE_CANARY_PCT_OMS`; `record_post_success_orphan(...)` helper for post-recipe-success write failures so the operator dashboard surfaces the audit-vs-OMS mismatch; per-connector CHANGELOG at `gateways/changelog/oms.md`). Live transport raises `NotImplementedError` until nightly `-m live` mark. |
| **PARITY-7** Spatial extraction hardening | **✓ shipped** (`gateways/document_extraction.py::_normalize` NFKC + soft-hyphen strip; `resolve_model_id` env-pin; `tests/eval/datasets/extraction_spatial/seed.jsonl` 1→12 rows across born_digital/scanned/multi_page/table_heavy; `tests/eval/thresholds.yaml::per_type` per-doc-type thresholds; eval-gate per-row scorer pairing fix; **drift-alert App Insights forwarder Job at `scripts/run_drift_forwarder.py` + `infra/main.bicep::driftAlertJob` cron Container Apps Job behind `deployDriftAlertJob`** — fires `evaluate_all_extraction_drift()` per tick and emits `extraction-drift` log events through the OTel exporter to App Insights). Live AzureDI HTTP transport still requires real Azure access. |
| **PARITY-8** Data governance closure | **✓ shipped** (`api/retention_sweeper.py` kill-switch + dry-run + residency-check + identity resolution + **real byte-wipe via `erase_attachment` with `SCHEDULED_RETENTION_DELETE` distinct audit row per candidate, absent-id no-op**; `contracts/policy.py::get_tenant_retention_ttl_days` per-tenant TTL; `compliance/dpia/_template.md` per-tenant DPIA; `docs/ops/erasure-flows.md` Mode A vs Mode B distinction + manual-replay refusal; `scripts/run_retention_sweeper.py` Container Apps Job CLI; `infra/main.bicep::retentionSweeperJob` cron Job gated on `deployRetentionSweeperJob` + `RETENTION_SWEEPER_ENABLED` two-step opt-in) |

---

## 0. Why this plan exists

The developer experience on **Vercel today** runs the Next.js UI with
`NEXT_PUBLIC_USE_REAL_API` unset. Every API call resolves out of the
in-repo mock layers (`MOCK_EXCEPTIONS` / `MOCK_ORDER_ANALYSES` /
`MOCK_LINE_ITEMS` / `INBOX_SECTION_BUNDLES`). The result is rich,
deterministic, and demoable — every section populates for every case.

The **Azure pre-prod target** runs the `asoe2` FastAPI backend on Azure
Container Apps with `NEXT_PUBLIC_USE_REAL_API=1`, so every API call hits
real Python code, real Postgres, real gateways.

Parity therefore splits into two arcs:

* **Arc A — demo parity (Phase 0).** The Azure UX matches the Vercel UX
  using the **sandbox stub gateways** already in
  `api/sandbox_gateways.py`.
* **Arc B — real-data parity (Phases 0.5–8).** Replace stubs with real
  Azure-native services and platform connectors, one seam at a time,
  each phase independently shippable and reversible.

Every phase has: **goal**, **deliverables** (concrete file paths),
**acceptance criteria** (testable), **risks**, **effort**, **dependencies**.

---

## 0.0 Expert review summary (2026-05-26)

Six independent expert reviews of v1 of this plan:

| Lens | Top blocker raised | Addressed in |
|---|---|---|
| **Azure Platform / SRE** | VNet + private endpoints; PgBouncer before Phase 6; startup probes vs cold-start budget; scaling rules | Phase 0, Phase 6 |
| **Security** | Separate signing keys (JWT vs attachment); JWKS aud/iss/kid pinning; Key Vault soft-delete + purge protection; CORS regex validation | Phase 0, Phase 3b, Phase 4 |
| **Integration / Data** | Per-gateway timeout/budget config; canary + shadow modes; DLQ; schema-evolution detection; fixture-capture process | Phase 6 |
| **Compliance / Audit** | **Audit-chain tombstone routing must precede any real-tenant deploy**; DPIA gate; proof-of-deletion certificate; retention-sweeper kill-switch | **new Phase 0.5**, Phase 8 |
| **Frontend / Identity** | Build-time provider mounting (NOT runtime); multi-env callback URIs; backend-authoritative role derivation; "logged in as" banner | Phase 3a |
| **ML / Document AI** | Golden set is one row — must expand to 10–15 diverse docs; model_id pinning + drift signal; cost guardrail reality check vs custom-extract | Phase 7 |

### Structural changes vs v1

* **Phase 0 + Phase 1 merged** into "Phase 0 — Foundation" (sub-phases 0a
  + 0b) — Security and Azure/SRE both flagged that fail-loud non-sandbox
  boot must ship with the first deploy, not after.
* **Phase 0.5 added** (Compliance) — audit-chain tombstone routing must
  land before any real-tenant pre-prod traffic. Without it, the first
  real erasure can't be proved to a regulator.
* **Phase 3 split** into 3a (UI scaffolding — parallel-safe from day 1)
  and 3b (backend JWKS + role mapping).
* **Phase 4 moved earlier** — runs in parallel with Phase 0; soft-delete
  + purge protection mandatory from day 1.
* **Phase 6 sub-phases reordered** — 6.1 Graph **gates** 6.2 DocAI; 6.3
  SAP runs in parallel with 6.2; 6.4 OMS last.

---

## Phase 0 — Foundation: sandbox-stub Azure pre-prod + fail-loud boot (M)

**Goal:** Pre-prod URL renders the same 36 cases the Vercel dev URL does
today (Phase 0a), and any non-sandbox boot fails loud rather than
running with an empty gateway registry (Phase 0b).

### Phase 0a — Demo parity via sandbox stubs

**Deliverables:** as v1.
* `.github/workflows/deploy-azure.yml`, `infra/main.bicep`,
  `scripts/deploy-azure.sh` — already present, run them.
* `asoe-ui` Container App env vars — as v1.

**Reviewer-mandated additions:**

* **[Azure/SRE]** Inject the Managed Environment's subnet, bind the
  Container App to a VNet, restrict Postgres firewall to that subnet
  only (today `infra/main.bicep:286–295` allows `0.0.0.0/0` — acceptable
  for sandbox but **not** for any tenant data).
* **[Azure/SRE]** Distinct startup probe with `initialDelaySeconds ≥ 60`
  (current Dockerfile.api `HEALTHCHECK start-period=20s` is too short if
  `apply_schema()` runs at boot). Measure actual cold start; if > 30s
  consistently, split migrations into a separate Container Apps Job that
  completes before the app revision starts.
* **[Azure/SRE]** Acceptance: container runs non-root (verify
  `whoami` in the deployed revision) and the ACR image passes a `trivy
  image` scan (or ACR native vulnerability scan) — gate adoption on no
  critical CVEs.
* **[Azure/SRE]** Set `connect_timeout=5` in `DATABASE_URL` to keep a
  Postgres outage from cascading into readiness-probe failure.
* **[Compliance]** Phase 0a ships only against **synthetic data**
  (sandbox stubs). Real-tenant pre-prod traffic is gated on Phase 0.5
  audit-chain integration landing first.
* **[Decision Q6+Q7]** Storage region: **East US 2**; replication: **LRS**;
  bicep `storageAccount.sku.name: 'Standard_LRS'`. (GA upgrades to per-tenant
  region + GRS.) `infra/main.bicep` Storage Account parameters fixed
  accordingly.
* **[Security]** Add a `Phase 0 security review` task: SSRF + XSS audit
  of the attachment download + signed-URL paths against
  `hardening/ssrf.py`; `detect-secrets` (or GitGuardian) pre-commit +
  CI; `pip-audit` (or `safety`) in CI failing on high/critical CVEs.
* **[Frontend]** Pin the exact preprod UI origin in
  `CORS_ALLOWED_ORIGINS` and **validate the regex at startup** — reject
  `.*` or obviously unsafe patterns; log the resolved allowlist at
  `INFO` on every boot. `NEXTAUTH_SECRET` persisted in Container App
  secrets (not `auto`) so revision restarts don't invalidate sessions.

### Phase 0b — Fail-loud non-sandbox boot + preprod gateway registration

**Goal:** A non-sandbox boot has a deterministic, defendable gateway
state — either an explicit `register_preprod_gateways()` (initially a
thin re-use of stubs) or a fail-loud refusal-to-start that names what
the platform team still has to wire.

**Deliverables:** as v1.

**Reviewer-mandated additions:**

* **[Security + Azure/SRE]** Invert the rule: boot **requires** an
  explicit, non-empty gateway registration for **any** env that isn't
  `sandbox` (not just `production`). Prevents the "oops, staging has no
  connectors" silent failure.
* **[Compliance]** The fail-loud message names the audit-bearing fields
  that won't populate, not just the missing gateway.

**Acceptance criteria** (combined 0a + 0b):

1. `tests/test_e2e_manual_order_intake_inbox_sections.py` runnable
   against the pre-prod backend — same 10/10 assertions pass.
2. Seed user logs into pre-prod UI; every Customer Inbox section
   populates (Source Email, Email Order Intake, Entities, SAP Data,
   Order Entry, EDI 850, Knowledge Graph, Draft Reply where applicable).
3. The 4 Playwright evidence journeys
   (`tests/browser/attachment-evidence.spec.ts`) run green against the
   pre-prod URLs.
4. `ASOE_ENV=preprod` boot succeeds against stubs; `ASOE_ENV=production`
   without `register_production_gateways` raises with a clear,
   actionable error before serving any request.
5. Container image scan green (no critical CVEs); non-root verified;
   secret-scan + dependency-audit CI gates green.
6. VNet + private-Postgres path commented into `infra/main.bicep` (even
   if the first deploy uses the sandbox firewall path for speed).

**Effort:** **M** (1 day; mostly tests + acceptance hardening).

**Dependencies:** none. Ship first.

---

## Phase 0.5 — Audit-chain tombstone routing (M — NEW per Compliance review)

**Goal:** Erasure tombstones (`erase_attachment` →
`get_erasure_tombstone`) move from the in-process registry into the
immutable, hash-chained `policy_audit_log` (ADR-023). **Required before
any real-tenant pre-prod data lands.**

**Deliverables:**

* `gateways/attachment_store.py` — `erase_attachment` writes the
  tombstone into `policy_audit_log` via `exception_store.log_audit_event`
  (or equivalent), with `event_type = "ATTACHMENT_ERASED"`. The
  in-process `_erasure_tombstones` registry remains as a fast-lookup
  cache; the audit chain is the source of truth.
* `compliance/audit_bearing_registry.yaml` — lock the tombstone schema
  `(attachment_id, sha256, tenant_id, case_id, size_bytes, mime_type,
  erased_at, erased_by_identity, reason)`. Restore the **CODEOWNERS**
  gate on the registry file.
* `api/routes/attachments.py` — new
  `GET /api/v1/attachments/{id}/erasure-certificate` that returns the
  chain-proof signed tombstone for regulator / customer disputes.
* `tests/test_attachment_erasure_audit_chain.py` — assert the tombstone
  lands in the chain, the chain verifies (`verify_audit_chain()`), and
  the certificate endpoint returns a tamper-evident response.

**Acceptance criteria:**

* `erase_attachment` followed by `verify_audit_chain()` returns
  `(True, None)` — the chain is intact.
* The erasure certificate is fetchable by `attachment_id` and embeds the
  hash-chain proof so a regulator can independently verify the deletion
  happened at the claimed time.
* CODEOWNERS reviewer required for any future tombstone-shape change.

**Risks:**

* The hash-chain INSERT throughput on heavy retention sweeps (Phase 8)
  could become a hot spot — mitigate by batched sweeper writes.

**Effort:** **M** (1–2 days).

**Dependencies:** Phase 0. Blocks: any real-tenant traffic.

---

## Phase 2 — Azure Blob Storage driver (M)

(Goal + bulk deliverables as v1.)

**Reviewer-mandated additions:**

* **[Security]** Bicep enforces a dependency chain: Container App (with
  Managed Identity) → Storage Account → RBAC assignment → Blob
  container. A startup probe calls the Blob API once before marking the
  app ready; fail the deploy if identity auth fails (don't rely on a
  lazy first-401-retry).
* **[Compliance]** Storage Account: `enableSoftDelete: true`, `versioning: true`,
  optional `geoReplication: true` (per-tenant residency check from
  Phase 8 gates this). Document the soft-delete retention SLA.
* **[Azure/SRE]** SBOM generated alongside the container image
  (`docker sbom` or syft); ACR signs images (cosign); deploy refuses
  unsigned images.

**Acceptance criteria** (combined):

* Portability contract test passes for `_AzureBlobStore` against a
  real Storage Account (nightly).
* Attachment uploaded via seed endpoint readable via the signed-URL
  endpoint after a Container App revision restart.
* Image signature verified at deploy time.

**Effort:** **M** (1–2 days). **Dependencies:** Phase 0.

---

## Phase 3a — Frontend identity scaffolding (M — split from Phase 3)

**Goal:** UI is provider-flexible from day 1: both `seed` (dev/CI) and
`azure-ad` (preprod) providers mounted, runtime-selected by env. Can run
**in parallel with Phase 0** — does not block on backend Entra work.

**Deliverables:**

* `asoe-ui/src/lib/auth.ts` — mount **both** providers in
  `authOptions.providers`; conditional instantiation guarded by
  `ASOE_AUTH_MODE` (build-time + runtime). **Do NOT** assume
  `NEXT_PUBLIC_AUTH_PROVIDER` is a runtime toggle — provider lists are
  frozen at NextAuth startup.
* `asoe-ui/src/auth/azure-ad.ts` — NextAuth Azure AD provider config
  (uses `SSO_CLIENT_ID` / `SSO_CLIENT_SECRET` / `SSO_ISSUER_URL`).
* `asoe-ui/src/components/ui/PreprodIdentityBanner.tsx` — non-dismissable
  banner shown whenever `ASOE_AUTH_MODE !== "seed"`:
  *"Preprod (Entra ID) — Logged in as: {user.email}"*. Mounted in
  `src/app/layout.tsx`.
* `asoe-ui/src/middleware.ts` — distinguish *token-missing* (→ `/login`)
  from *token-invalid* (→ `/login?reason=session_expired`) and *no role
  for tenant* (→ `/403` with explanatory copy, not generic logout).
* `docs/testing/auth-modes.md` — auth-mode matrix (seed vs Entra) for
  dev / Vercel / preprod, with Playwright fixture strategy per mode.

**Decision Q8 — App Registration shape:** **one** App Registration named
`asoe-ui`, with the following redirect URIs registered up-front:
* `http://localhost:3000/api/auth/callback/azure-ad` (dev)
* `http://localhost:3100/api/auth/callback/azure-ad` (Playwright)
* `https://<vercel-preview-domain>/api/auth/callback/azure-ad` (Vercel)
* `https://<asoe-ui-preprod>.azurecontainerapps.io/api/auth/callback/azure-ad` (Azure preprod)
* (Future GA URIs added as deployed.)

**Acceptance criteria:**

* Both providers coexist in the same `authOptions.providers` without
  collision; switching `ASOE_AUTH_MODE` between deploys flips behaviour
  with no source change.
* Mock-mode Playwright suite (existing) still green; new fixtures cover
  the three middleware branches (missing / invalid / no-role).
* OAuth callback works on three URLs (localhost dev, Vercel preview,
  Azure pre-prod) without per-environment Entra App Registration edits.

**Effort:** **M** (2 days). **Dependencies:** Phase 0 acceptance for the
deployed UI URL, but coding can start earlier.

---

## Phase 3b — Backend Entra ID JWKS validation + role mapping (L)

**Goal:** Replace HS256 + seed users with RS256 + Entra ID JWKS-validated
tokens, with Entra group → ASOE role mapping. Seed path preserved
behind `ASOE_AUTH_MODE=seed`.

**Deliverables:** as v1, plus the reviewer-mandated additions below.

**Reviewer-mandated additions:**

* **[Security — CRITICAL]** JWKS validation completeness:
  * **Pin** `aud` against `ASOE_CLIENT_ID` env var.
  * **Pin** `iss` against `ASOE_ISSUER_URL` env var (rejects
    cross-tenant tokens immediately).
  * **Pin** `kid` against the JWKS — refresh JWKS on unknown `kid`.
  * JWKS cache: 1h TTL + refresh on 401; fail closed on JWKS-fetch
    timeout.
  * Add a regression test: a token whose `iss` is from a different
    Entra tenant returns 403 with a logged warning.
* **[Security]** Refresh-token strategy: stored in HttpOnly +
  Secure-flagged cookie (not localStorage); rotation on refresh; a
  revocation table (`refresh_token_revocations`) to invalidate on
  password change / role change. Refresh endpoint validates the old
  token before issuing a new one.
* **[Frontend]** Backend's `roles` claim shape is the contract: an Entra
  group ID → role name mapping happens in `api/azure_ad_roles.py`; the
  UI continues to read `user.roles` unchanged. Lock the contract in
  `tests/architectural/auth_contract.test.ts` so a backend shape change
  doesn't silently break the UI.
* **[Frontend]** Role-change handling: when a user's group membership
  changes mid-session, the next token refresh propagates the new roles.
  If the new roles strip access to the current page, middleware
  redirects to `/403` rather than silently failing API calls.

**Decision Q1 — tenancy:** App Registration **`signInAudience: 'AzureADMyOrg'`**
for preprod (single Entra tenant). A GA follow-up ADR will switch to
`signInAudience: 'AzureADMultipleOrgs'` and add the per-tenant consent
flow + cross-tenant `iss` validation. Phase 3b must explicitly reject
tokens whose `iss` claim is from any tenant other than the configured
preprod tenant (`ASOE_ISSUER_URL`) — this is the gate that contains
single-tenant blast radius today.

**Acceptance criteria:** v1 criteria plus the three regression tests
above (cross-tenant iss rejection; revocation; role-downgrade UX).

**Effort:** **L** (3–5 days). **Dependencies:** Phase 0 + Phase 3a
contract.

---

## Phase 4 — Key Vault secrets + separate signing keys (M — moved earlier)

**Goal:** Stop passing `ASOE_JWT_SECRET`, `DATABASE_URL`, LLM provider
keys as plaintext Container App env vars; introduce **separate signing
keys** for JWT vs the attachment capability token.

**Reviewer-mandated additions:**

* **[Security + Compliance — CRITICAL]** Key Vault provisioned with:
  * `enableSoftDelete: true`
  * `softDeleteRetentionInDays: 90`
  * `enablePurgeProtection: true`
  * RBAC mode (no access policies).
* **[Security — CRITICAL]** **Separate** signing keys:
  * `ASOE_JWT_SECRET` — HS256 fallback for `ASOE_AUTH_MODE=seed`; not
    used in Entra mode.
  * `ASOE_ATTACHMENT_SIGNING_KEY` — distinct secret for
    `api/attachment_read_token.py`. Independently rotatable.
  * Plan to migrate the attachment token to an asymmetric key
    (ES256/EdDSA) in a follow-up so rotation is transparent.
* **[Security]** Key rotation handles the overlap window: secondary
  key slot accepted on verify; primary used on sign. Test in
  `tests/test_signing_key_rotation.py` that an old-signed token still
  validates during the rotation window.
* **[Azure/SRE]** Build-time vs runtime config boundary documented:
  static (image-baked) — `ASOE_LLM_PROVIDER` default,
  `ASOE_KILL_SWITCH=0`, `ASOE_EXPLAIN_MODE=0`; runtime (Key Vault) —
  every secret + per-deploy override.
* **[Compliance]** Break-glass recovery key stored in a separate Key
  Vault (different RBAC).
* **[Decision Q4]** Preprod rotation cadence: **manual operator-triggered**
  (documented runbook in `docs/ops/secrets-rotation.md`); a Container App
  revision restart picks up the new secret. **GA follow-up:** a 90-day
  automated rotation policy via Azure Function callback. Tracked in
  `docs/plans/ga-preconditions.md` (now shipped — tracks every preprod→GA
  deferral, see that file for the full table).

**Acceptance criteria:** v1 plus:

* `az containerapp show` returns no plaintext secret values for the
  signing keys.
* Rotation test green: a freshly minted token under the new key still
  validates an old refresh token from before rotation (overlap window).

**Effort:** **M** (1–2 days). **Dependencies:** Phase 0 (can run in
parallel).

---

## Phase 5 — Azure Monitor / Application Insights (M)

(Bulk as v1.)

**Reviewer-mandated additions:**

* **[Azure/SRE]** Pre-define alert names in Phase 0 so Phase 5 can fill
  in thresholds without scope creep: `zero-highlight`,
  `layer2-open-rate`, `breaker-open`, `extraction-cost-overrun`,
  `audit-chain-verify-failed`, `retention-sweeper-anomaly`.
* **[Security]** Annotate audit-bearing routes with an `@audit_bearing`
  decorator; CI lint fails if a state-mutating route lacks it.
* **[Azure/SRE]** Log redaction for PII at the structured-logger level
  (`api/observability/`); the Application Insights exporter never sees
  plaintext PII.
* **[Decision Q5]** Workspace: a **dedicated** Application Insights +
  Log Analytics workspace for preprod (`asoe-preprod-monitoring`);
  separate billing + retention from any other org Azure project.
  Retention: 90 days for App Insights, 30 days for Log Analytics
  (overridable per query). Bicep parameter `monitoringWorkspaceMode:
  'dedicated'`.

**Effort:** **M** (2–3 days). **Dependencies:** Phase 0.

---

## Phase 6 — Real platform connectors (XL aggregate, reordered)

**Updated ordering** (Integration review): 6.1 Graph **gates** 6.2 DocAI
(DocAI reads bytes Graph supplies); 6.3 SAP runs **in parallel** with
6.2; 6.4 OMS last (depends on SAP validation).

```
Phase 6.1 Graph ─┬─► 6.2 Document Intelligence ─┐
                 │                                ├─► 6.4 OMS
                 └─► 6.3 SAP (× 5 domains) ──────┘
                 │
                 └─► 6.5 KG / EDI / Change (no-op — already pure)
```

**Reviewer-mandated additions (apply to every sub-phase):**

* **[Azure/SRE]** PgBouncer (or Azure-native connection pooling) wired
  **before** sub-phase 6.3 ships — real SAP fan-out will exhaust the
  default pool. Recommended: PgBouncer sidecar in the Container App;
  transaction mode.
* **[Integration]** Per-gateway timeout + budget constants in
  `contracts/policy.py`: Graph ~3s, SAP ~8s, OMS ~5s, DocAI per-page
  cost ceiling (see Phase 7 reality-check).
* **[Integration]** Fixture-capture process documented:
  `docs/ops/fixture-capture.md` — who runs `scripts/record_gateway.py`,
  on what sanitized data, on what cadence. PII-redacted before commit.
* **[Integration]** Per-connector CHANGELOG so schema evolution is
  attributable. Nightly fixture-vs-live diff alerts on drift.
* **[Integration]** Canary deploy: a single real connector behind a
  feature flag (5% → 25% → 100% over a week) with auto-rollback on
  latency/failure-rate breach.
* **[Integration]** Shadow mode: optional A/B where real and stub both
  run; output shape diff'd into a compliance-reviewable audit log
  before flipping any composer to real-only.
* **[Integration]** Dead-letter queue for poison messages (Graph 429s
  past retry, SAP OData pool exhaustion, OMS write failures
  post-recipe-success). Operator dashboard to surface pending
  compensations from `orchestration/outbox.py`.
* **[Security + ML]** PII redaction on egress to AzureDI (6.2): mask
  customer credit fields, addresses outside the field-of-interest,
  before sending. Compliance review gate.

**Decision Q3 — Phase 6.3 SAP environment:** **real S/4HANA preprod
tenant, read-only credentials** (pending SAP Basis team confirmation).
Documented fallback: SAP Cloud trial / dev tenant if access is delayed
past Phase 6.3 spec lock. Either way, Phase 6.3 ships against a
schema-realistic source, not stubs.

**Decision Q2 — Phase 6.2 model choice:** Azure Document Intelligence
**prebuilt invoice + custom post-process** (`prebuilt-invoice` model
pinned via `ASOE_DOCUMENT_EXTRACTION_MODEL_ID`). Phase 7 acceptance on
the expanded golden set gates any upgrade to custom-extract; if
upgrade is needed, raise `EXTRACTION_MAX_COST_USD_PER_PAGE` to 0.15
in the same PR.

**Decision Q9 — shadow-mode acceptance** (codified as
`tests/eval/shadow_mode_thresholds.yaml`):
* Audit-bearing fields (declared in `compliance/audit_bearing_registry.yaml`):
  **0% diff** vs stub — exact match required per field per event.
* Derived / contextual fields: **≤ 5% diff** — accommodates formatting
  variance (date strings, free-text descriptions).
* Optional fields: not gated.
* Window: **7 consecutive days** of shadow runs with **< 1%**
  audit-bearing-diff rate before a connector flips to real-only.
* Per-tenant overrides land as `thresholds.yaml` rows; lowering a
  threshold needs reviewer sign-off (Integration + Compliance).

**Per-sub-phase acceptance** (beyond replay-passes-green):

* Nightly `-m live` gate green for **7 consecutive days** (schema
  stable, permissions unchanged) — matches the Q9 window.
* **Latency parity**: real p50 + p99 ≤ 1.2× stub p50.
* **Failure-rate parity**: real failure rate ≤ stub-equivalent under
  100 injected-fault scenarios (breaker + timeout matrix).
* **Fixture completeness**: 100% of preprod inbound event types have at
  least one recorded fixture per connector per operation.
* **Shadow-mode diff**: meets the Q9 thresholds across the 7-day window.

**Effort:** **XL** aggregate; sub-phases **M–L**.
**Dependencies:** Phase 0 (specifically the fail-loud preprod boot
from Phase 0b).

---

## Phase 7 — Live spatial extraction (M, hardened per ML review)

**Goal:** Close ADR-045's deferred live path with Azure Document
Intelligence.

**Reviewer-mandated additions — these gate the live path:**

* **[ML — CRITICAL]** Golden-set expansion **before** live shipping:
  current `tests/eval/datasets/extraction_spatial/seed.jsonl` has **one
  row** (born-digital PO_8842). Expand to **≥10–15 rows** spanning: (i)
  born-digital, (ii) scanned single-page, (iii) multi-page, (iv)
  table-heavy. Hand-label scanned ground-truth bboxes. The current set
  is a development prototype, not a production gate.
* **[ML]** `ASOE_DOCUMENT_EXTRACTION_MODEL_ID` env var pins the AzureDI
  model (e.g. `prebuilt-invoice-4-3` or `custom-eur-po-2026-05-01`); a
  model bump requires a deliberate env-var update + a fresh golden-set
  rerun.
* **[ML]** Drift signal alert: 7-day rolling median containment drop
  > 5pp on the same doc set fires `extraction-drift`. Tied to the
  `extraction_mean_confidence` / `extraction_canary_containment` meters
  already shipped (`api/metrics.py`).
* **[ML]** Shared text-normalisation contract between
  `document_extraction.py::_normalize` and AzureDI output: define
  handling for soft hyphens (U+00AD), ligatures (fi, fl), unicode NFC
  vs NFD. Encode the choice in one function; unit-test the edge cases.
  Today's verifier will silently degrade-to-text on a ligature mismatch.
* **[ML]** Confidence calibration sweep on the expanded golden set:
  AzureDI's per-field confidence is **not** calibrated to our
  containment definition. If the calibration slope is poor (Brier > 0.1),
  recalibrate or replace AzureDI confidence with our own ECE score
  before using it in the live gate.
* **[ML]** Cost guardrail reality check: current default
  `EXTRACTION_MAX_COST_USD_PER_PAGE = 0.05` may be too low for
  custom-extract (typical p95 $0.08–$0.15/page). Run a pricing
  simulation on 50 real POs **before** the provider decision locks
  (open question #2). Raise the ceiling to a realistic value or commit
  to prebuilt invoice + selection.
* **[ML]** Per-document-type acceptance: table-heavy vs single-line vs
  multi-page each get their own containment / hallucination ceiling in
  `thresholds.yaml`.
* **[ML]** Canary: roll spatial overlays to 1% of preprod traffic
  first; compare actual containment to the replay gate before 100%
  cutover.

**Clarification (ML review):** Phase 6.2 ships
`DocumentExtractionGateway` against the **recorded backend** only.
Phase 7 wires the **live AzureDI backend** behind the same `propose(...)`
seam and runs the `tests/eval -m live` gate. They are two
backend swaps, not two flows.

**Effort:** **M** for live wiring; **+S–M** to expand the golden set
(may be the long pole).
**Dependencies:** Phase 6.2 (recorded backend) + golden-set expansion.

---

## Phase 8 — Data governance (L, expanded per Compliance review)

**Goal:** Close ADR-044's deferred governance items so GA is unblocked.

**Reviewer-mandated additions:**

* **[Compliance — CRITICAL]** A DPIA gate before Phase 8 enables for
  any real tenant: documents what data is deleted, at what TTL, under
  which legal basis, with what residual audit obligations. Recorded in
  `compliance/dpia/{tenant-id}.md`.
* **[Compliance]** Per-tenant DPA / residency check: the retention
  sweeper refuses to delete from a region that violates a tenant's
  residency commitment (declared in `contracts/policy.py` per-tenant
  config) — fail loud, log to audit, alert the operator.
* **[Compliance]** Retention-sweeper kill-switch:
  `RETENTION_SWEEPER_ENABLED=true|false` env var; human-in-the-loop
  dry-run produces an audit-logged plan; operator must confirm before
  the sweeper commits. Default in preprod = disabled.
* **[Compliance]** Distinct `SCHEDULED_RETENTION_DELETE` audit event
  type, so bulk sweeper deletes are visible in the audit trail and
  distinguishable from operator-requested erasures.
* **[Compliance]** Proof-of-deletion certificate endpoint
  (`GET /api/v1/attachments/{id}/erasure-certificate`) — already
  introduced in Phase 0.5; here we add tenant-facing UI to download it.
* **[Compliance]** Erase-customer-data vs erase-content-and-keep-
  attestation distinction documented in `docs/ops/erasure-flows.md`.
  Different operator UIs; different audit-event sub-types.
* **[Compliance]** Identity-resolution order on the tombstone:
  (1) authenticated user from JWT; (2) Entra ID lookup with fallback;
  (3) `system:service-principal` for the sweeper. Always recorded.
* **[Compliance]** Manual-replay refusal documented: a customer-disputed
  deletion does **not** restore from backup; the audit chain proves the
  deletion happened. The dispute response is the certificate, not the
  bytes.

**Acceptance criteria:** v1 criteria plus DPIA recorded per tenant,
kill-switch verified, certificate endpoint round-trip, residency-check
test green.

**Effort:** **L** (5–7 days). **Dependencies:** Phases 0.5 + 2 + 4.

---

## Parallelism map (updated)

```
Phase 0   ─────────► (sandbox stubs + fail-loud boot — single ship gate)
   │
   ├─► Phase 0.5 ────► (audit-chain tombstone routing — gates real-tenant data)
   │       │
   │       ▼
   ├─► Phase 2 ───────► (Azure Blob)        ─┐
   ├─► Phase 3a ───► Phase 3b ──► (Entra ID) ├─► Phase 6 ─┬─► Phase 7
   ├─► Phase 4 ───────► (Key Vault)         ─┤            │
   ├─► Phase 5 ───────► (Az Monitor)        ─┘            │
   │                                                       │
   └────────────────────────────► Phase 8 ◄────────────────┘
```

* Phase 0 ships first.
* Phase 0.5 gates real-tenant traffic on Phase 0.
* Phases 2 / 3a / 4 / 5 run in parallel after Phase 0.
* Phase 3b depends on 3a's contract.
* Phase 6 needs the fail-loud preprod boot from Phase 0b.
* Phase 7 needs Phase 6.2 + golden-set expansion.
* Phase 8 needs Phases 0.5 + 2 + 4.

---

## Phase ↔ Parity matrix

| Capability | Vercel Dev (today) | Azure Pre-Prod (target) | Closing phase |
|---|---|---|---|
| Inbox 9 cases populated | Mock bundles | Sandbox stubs → backend `enrichment_context` | Phase 0a |
| Evidence Detail line items | `MOCK_LINE_ITEMS` | `_project_line_items` from event | Phase 0a |
| Attachment preview + safety bar | Mock bytes + mock anchors | Seed endpoint or real ingestion | Phase 0a / 6.1 |
| Audit-chain proof of erasure | n/a | Tombstone in `policy_audit_log` + certificate endpoint | **Phase 0.5** |
| Auth | Seed users + any-password | Entra ID + group → role | Phase 3a + 3b |
| Attachment bytes durability | n/a | Azure Blob + soft-delete | Phase 2 |
| Secrets | n/a | Key Vault (soft-delete + purge-protected) + separate signing keys | Phase 4 |
| Metrics / traces | n/a | App Insights + Log Analytics + pre-named alerts | Phase 5 |
| Email source-of-truth | Mock email body | Microsoft Graph + PII redaction | Phase 6.1 |
| SAP data accuracy | Mock | Real S/4HANA OData behind PgBouncer | Phase 6.3 |
| Spatial overlays | Mock anchors | Live Document Intelligence — golden-set expanded | Phase 7 |
| Retention / DPIA / residency | n/a | Per-tenant TTL + DPIA + sweeper kill-switch | Phase 8 |

---

## Risk register (updated)

| Risk | Mitigation | Owner |
|---|---|---|
| Silent empty registry on non-sandbox boot | Phase 0b — fail-loud for **any** non-sandbox env (not just production) | Backend |
| Postgres exposed via `0.0.0.0/0` | Phase 0a — VNet + private endpoint commented in (used from first real-tenant deploy) | Platform |
| Postgres connection-storm under Phase 6 fan-out | PgBouncer sidecar wired before Phase 6.3 | Platform |
| JWT and attachment-token share one signing key | Phase 4 — separate `ASOE_ATTACHMENT_SIGNING_KEY` | Security |
| Cross-tenant Entra token accepted | Phase 3b — `aud` + `iss` + `kid` pin + regression test | Security |
| Key Vault accidental delete unrecoverable | Phase 4 — soft-delete + purge-protection mandatory | Security + Compliance |
| First real erasure can't be proved to a regulator | **Phase 0.5** must precede any real-tenant data | Compliance |
| AzureDI model bump silently moves boxes | Phase 7 — `ASOE_DOCUMENT_EXTRACTION_MODEL_ID` pin + drift alert | ML |
| Cost guardrail too low for custom-extract | Phase 7 — pricing sim on 50 real POs before provider lock | ML |
| Single-row golden set won't generalise | Phase 7 — expand to ≥10–15 rows covering scanned + multi-page + tables | ML |
| Provider runtime switch breaks NextAuth session | Phase 3a — providers always mounted; env gates instantiation, not the mount | Frontend |
| Retention sweeper deletes too aggressively | Phase 8 — `RETENTION_SWEEPER_ENABLED` + dry-run + operator confirm | Compliance |
| Container image ships with known CVEs | Phase 0a — `trivy` (or ACR native) scan gate; SBOM + cosign | Platform + Security |
| `NEXTAUTH_SECRET` lost on UI revision recreate | Phase 0a — persist in Container App secrets (later Key Vault, Phase 4) | Frontend |

---

## Rollback / safety net

Every Phase 0.5–8 change is **env-flagged**: clearing the env var falls
back to the previous behaviour. Specifically:

* `ASOE_ENV=sandbox` → sandbox stub registration.
* `ASOE_OBJECT_STORE_DRIVER=filesystem` → no Blob driver.
* `ASOE_AUTH_MODE=seed` → HS256 + seed users.
* `ASOE_DOCUMENT_EXTRACTION_BACKEND=recorded` → no live AzureDI call.
* `RETENTION_SWEEPER_ENABLED=false` → no automated deletes.
* Unset `APPLICATIONINSIGHTS_CONNECTION_STRING` → no OTel.
* Unset `ASOE_EMAIL_INTAKE_DRIVER` etc. → real-connector phases revert
  to stubs.

The existing `deploy-azure.yml` 60-second healthcheck rolls back to
the previous Container App revision on failure. Every phase ships with
a regression test that fails on its parent commit.

---

## Decisions log (2026-05-26 — all 9 v2 open questions resolved)

| # | Question | Phase | **Decision** | Rationale |
|---|---|---|---|---|
| 1 | Entra ID tenancy model | 3b | **Single-tenant for preprod; switch the App Registration to multi-tenant for GA.** | Preprod has ~one customer-equivalent; single-tenant is simpler. A documented migration path to multi-tenant is required so the App Registration boundary doesn't have to be torn down at GA. |
| 2 | AzureDI model choice | 6.2 / 7 | **Start with prebuilt invoice + custom post-process (~$0.01/page); upgrade to custom-extract only if the expanded golden set (Phase 7) shows insufficient accuracy on customer-specific layouts.** | Fastest to ship; cheapest; keeps the existing `EXTRACTION_MAX_COST_USD_PER_PAGE = 0.05` ceiling realistic. Phase 7 acceptance gates the upgrade decision on real data. |
| 3 | SAP environment for preprod | 6.3 | **Real S/4HANA preprod tenant, read-only credentials** (subject to SAP Basis team confirming access). Fallback if access is delayed: SAP Cloud trial / dev tenant. | Most realistic schema; catches drift early; read-only contains blast radius. |
| 4 | Key Vault rotation cadence | 4 | **Manual operator-triggered rotation for preprod; 90-day automated policy via Azure Function callback + Container App revision restart is a GA follow-up.** | Standard for preprod; defers automation cost to GA. Soft-delete + purge-protection mandatory from day 1 either way. |
| 5 | App Insights workspace | 5 | **Dedicated workspace for preprod** (separate billing + retention from other Azure projects). | Clean blast radius; cost is marginal vs the clarity gain. Can fold into a shared org workspace later if FinOps mandates. |
| 6 | Attachment-byte residency (preprod) | 2 / 8 | **Single region (Azure East US 2), LRS storage for preprod.** | Cheapest; preprod runs synthetic / internal data — no DPA-driven residency yet. |
| 7 | Storage replication strategy | 2 / 8 | **LRS for preprod; per-tenant region selection + GRS for GA.** Tombstone-replication discipline (Compliance review) lands with GRS. | Right cost-vs-resilience for each phase; GA gate makes the GRS-tombstone story explicit. |
| 8 | OAuth App Registration strategy | 3a | **One App Registration per logical app (`asoe-ui`); multiple redirect URIs registered (localhost dev, Vercel preview, Azure preprod, future GA URLs).** | Standard SaaS pattern; one App identity per app; adding an environment = adding a URI, not a new registration. |
| 9 | Shadow-mode acceptance | 6 | **Audit-bearing fields: 0% diff vs stub (exact match). Derived/contextual fields: ≤5% diff. Optional fields: not gated.** Shadow runs **7 consecutive days under 1% audit-bearing diff** before flipping a connector to real-only. | Audit-bearing fields drive SOX attestations — no tolerance there. Contextual fields (formatted dates, free-text descriptions) tolerate cosmetic variance. The 7-day window mirrors the same eval rigor used elsewhere in the codebase. |

These decisions are folded into the relevant phase sections below; see
each phase for the concrete env-var settings + bicep parameters that
follow.

---

## Outstanding decisions (none blocking preprod stand-up)

None. All v2 open questions are resolved or have clear expert defaults
adopted. Two operational dependencies remain to be confirmed by the
platform team but do not block the plan itself:

* **Q3 confirmation** — SAP Basis team approves read-only access to the
  preprod S/4HANA tenant. Fallback: SAP Cloud trial tenant + a written
  note in Phase 6.3 ticket.
* **GA multi-tenant Entra migration** — out of preprod scope; will be a
  dedicated ADR when the second customer onboards.

---

## Out-of-scope (this plan)

* **GA-only items** beyond Phase 8 (multi-tenant Entra, customer-managed
  HSMs, hot-reload of Key Vault secrets without revision restart).
* **LLM provider migration** — orthogonal to deployment parity.
* **Web/mobile distribution** — only Container App URL parity in scope.

---

## Suggested first ticket-set (updated)

1. **PARITY-0** Phase 0 — sandbox-stub Azure deploy + fail-loud boot
   + VNet/private-endpoint comment + image-scan gate + secret-scan CI
   (S–M). **Ship first.**
2. **PARITY-0.5** Phase 0.5 — audit-chain tombstone routing + erasure
   certificate endpoint + CODEOWNERS gate on the registry (M).
   **Blocks real-tenant traffic.**
3. **PARITY-2** Phase 2 — Azure Blob driver + Managed Identity
   health-check (M). Parallel-safe after Phase 0.
4. **PARITY-3A** Phase 3a — NextAuth dual-provider scaffolding +
   preprod identity banner + multi-env callback test (M). Parallel-safe.
5. **PARITY-3B** Phase 3b — backend JWKS + role mapping + refresh-
   token revocation (L). Depends on 3a contract.
6. **PARITY-4** Phase 4 — Key Vault (soft-delete + purge-protection)
   + separate signing keys (M). Parallel-safe.
7. **PARITY-5** Phase 5 — App Insights + OTel + pre-named alerts +
   `@audit_bearing` decorator (M). Parallel-safe.
8. **PARITY-6.x** Phase 6 sub-tickets — one per connector domain;
   start with Graph (gates DocAI); PgBouncer pre-req (L each).
9. **PARITY-7** Phase 7 — golden-set expansion + live Document
   Intelligence + drift alert + cost reality-check (M).
10. **PARITY-8** Phase 8 — governance: DPIA + residency check +
    sweeper kill-switch + erasure-flow docs (L).

Each ticket lands as a self-contained PR with a regression test that
fails on its parent commit. The plan is scoped so any phase can be
deferred without blocking the others (modulo `Phase 0 → all`,
`Phase 0.5 → real-tenant data`, `Phase 1-equivalent (now 0b) → Phase 6`,
`Phase 6.1 → 6.2`, and `Phase 6.2 + golden-set → Phase 7`).
