# Azure Pre-Prod ↔ Vercel Dev Parity Plan

**Status:** Draft (2026-05-26)
**Owner:** Platform + Backend + Frontend (joint).
**Scope:** Bring Azure pre-prod to feature parity with today's Vercel dev
(mocked-layers) UX, then progressively unlock real-data pre-prod.

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
  `api/sandbox_gateways.py`. This is enough to validate the whole Azure
  pipeline (bicep, OIDC, ACR, Container Apps, Postgres, health-check,
  rollback) without any new data-plane code or real connectors.
* **Arc B — real-data parity (Phases 1–8).** Replace stubs with real
  Azure-native services and platform connectors, one seam at a time,
  each phase independently shippable and reversible.

Every phase is structured with: **goal**, **deliverables** (concrete
file paths), **acceptance criteria** (testable), **risks**, **effort**
(T-shirt sized), **dependencies**. Effort sizes are calibrated against
this codebase's recent commits — they're rough but ordered.

Cross-references to current state are file:line where they help.

---

## Phase 0 — Demo parity: Azure pre-prod with sandbox stubs (S)

**Goal:** The pre-prod URL renders the same 36 cases (9 inbox + 27
non-inbox) the Vercel dev URL does today, with every evidence section
populated. No new data-plane code; just exercise existing infra +
sandbox stubs.

**Deliverables:**

* `.github/workflows/deploy-azure.yml` — already present (Container
  Apps + OIDC + ACR + health-check poll + rollback). Run it.
* `infra/main.bicep` — already provisions ACR + Postgres Flexible +
  Redis + Log Analytics + Managed Env + Container Apps. Deploy with:
  * `asoeEnv: 'sandbox'` — so `api/app.py:156` triggers
    `register_sandbox_gateways()`.
  * `deployUiContainerApp: true`.
  * `databaseUrl` — Azure Postgres connection string with `sslmode=require`.
  * `jwtSecret` — strong random (≥64 chars) via `@secure()` for Phase 0;
    moves to Key Vault in Phase 4.
* `asoe-ui` Container App env vars:
  * `NEXT_PUBLIC_API_URL=https://<asoe2>.azurecontainerapps.io`
  * `NEXT_PUBLIC_USE_REAL_API=1`
  * `NEXTAUTH_URL=https://<asoe-ui>.azurecontainerapps.io`
  * `NEXTAUTH_SECRET` (random, ≥32 chars)

**Acceptance criteria:**

1. The committed contract test
   `tests/test_e2e_manual_order_intake_inbox_sections.py` runnable
   against the pre-prod backend (set `E2E_BACKEND_URL`) — same 10/10
   assertions pass.
2. Seed user `marcus.webb@acme-corp.com` (admin) logs into the pre-prod
   UI and opens a Customer Inbox case → every section populated (Source
   Email, Email Order Intake, Entities, SAP Data, Order Entry, EDI 850,
   Knowledge Graph, Draft Reply where applicable).
3. The 4 Playwright evidence journeys (`tests/browser/attachment-evidence.spec.ts`)
   run green against the pre-prod URLs.

**Risks:**

* Seed users are committed source (no real auth). Acceptable for Phase 0
  demo; Phase 3 fixes.
* `get_shared_adapter().apply_schema()` runs on every cold start —
  idempotent on Postgres but adds startup latency; observe + budget.

**Effort:** **S** (hours; mostly env-var plumbing + a deploy run).

**Dependencies:** none. **Phase 0 must ship before any other phase.**

---

## Phase 1 — Fail-loud non-sandbox boot + preprod gateway registration (M)

**Problem:** `api/app.py:156–161` only calls
`register_sandbox_gateways()` when `ASOE_ENV=sandbox`. Any other env
boots with an **empty** gateway registry → the first `/resolve` call
fails with `KeyError: Gateway not registered: <name>`. This is silent
and confusing.

**Goal:** A non-sandbox boot has a deterministic, defendable gateway
state — either an explicit `register_preprod_gateways()` (initially a
thin re-use of stubs) or a fail-loud refusal-to-start that names what
the platform team still has to wire.

**Deliverables:**

* `api/preprod_gateways.py` — new module mirroring the shape of
  `api/sandbox_gateways.py`. Initially imports and re-uses the same
  StubGateway instances. Mounted when `ASOE_ENV=preprod`.
* `api/production_gateways.py` — skeleton that raises
  `NotImplementedError("register_production_gateways must be wired by "
  "platform-team before ASOE_ENV=production boot")` so a misconfigured
  production deploy fails loud, not silently.
* `api/app.py` — extend the boot branch:
  ```python
  if env == "sandbox":
      register_sandbox_gateways()
  elif env == "preprod":
      from api.preprod_gateways import register_preprod_gateways
      register_preprod_gateways()
  elif env == "production":
      from api.production_gateways import register_production_gateways
      register_production_gateways()  # raises until platform wires real connectors
  ```
* Tests:
  * `tests/test_preprod_gateway_registration.py` — `ASOE_ENV=preprod`
    app boots; `/resolve` succeeds against stubs.
  * `tests/test_production_gateway_registration.py` —
    `ASOE_ENV=production` boot raises `NotImplementedError` with a clear
    message until a real implementation lands.

**Acceptance criteria:**

* The contract test from `tests/test_e2e_manual_order_intake_inbox_sections.py`
  passes under `ASOE_ENV=preprod` unchanged.
* `ASOE_ENV=production` boot fails with a clear, actionable error
  pointing at `api/production_gateways.py`.

**Risks:** doubles the surface to keep in sync — mitigate by keeping
`preprod_gateways.py` a thin re-export of `sandbox_gateways.py` until
real connectors land per gateway (Phase 6).

**Effort:** **M** (1 day; mostly tests + the fail-loud production boot).

**Dependencies:** Phase 0.

---

## Phase 2 — Azure Blob Storage driver (M)

**Goal:** Native Azure Blob driver behind the existing `_BlobStore`
seam (`gateways/attachment_store.py`), authed via the Container App's
managed identity (no plaintext keys).

**Deliverables:**

* `pyproject.toml` — add an optional extra:
  ```toml
  [project.optional-dependencies]
  azure = ["azure-storage-blob>=12.19", "azure-identity>=1.16"]
  ```
* `gateways/attachment_store.py`:
  * `_azure_blob_store()` — mirror `_s3_blob_store()` shape (lazy
    import; live-only). Uses `DefaultAzureCredential`; container name
    from `ASOE_OBJECT_STORE_BUCKET`; account URL from
    `ASOE_OBJECT_STORE_ENDPOINT`.
  * `_select_backend` — recognise `ASOE_OBJECT_STORE_DRIVER=azure`.
* `tests/test_attachment_store_portability.py` — extend the parity loop
  to cover the Azure driver (skip when `azure-storage-blob` not
  installed; live in nightly).
* `.env.example` — document the new driver.
* `infra/main.bicep` — provision a Storage Account + container; assign
  the Container App's managed identity `Storage Blob Data Contributor`
  on the container.

**Acceptance criteria:**

* Storage-portability contract test passes for `_AzureBlobStore`
  against a real Storage Account (nightly only).
* End-to-end durability: an attachment uploaded via the seed endpoint
  is still readable via the signed-URL endpoint after a Container App
  revision restart.

**Risks:** managed-identity propagation timing — `DefaultAzureCredential`
needs the Container App identity provisioned before first request.
Mitigate by lazy-importing and retrying on first 401.

**Effort:** **M** (1–2 days).

**Dependencies:** Phase 0.

---

## Phase 3 — Azure AD / Entra ID identity (L)

**Goal:** Replace the 5 seed users in `api/users.py` + dev HS256 JWT
secret with Entra ID end-to-end: NextAuth Azure AD provider on the UI;
JWKS-validated tokens on the backend; Entra ID group → ASOE role
mapping. The seed-user path stays for sandbox/CI/dev.

**Deliverables (UI):**

* `asoe-ui/src/auth/azure-ad.ts` — NextAuth Azure AD provider using
  `SSO_CLIENT_ID` / `SSO_CLIENT_SECRET` / `SSO_ISSUER_URL` already in
  `.env.local.example`.
* `NEXT_PUBLIC_AUTH_PROVIDER` flag: `azure-ad` (preprod) | `seed`
  (default — dev/CI).

**Deliverables (Backend):**

* `asoe2/api/deps.py` — extend `get_current_user` to validate against
  Entra ID JWKS when `ASOE_AUTH_MODE=entra` (default `seed` preserves
  the dev path). HS256 stays the seed path; RS256 + JWKS the Entra path.
* `asoe2/api/azure_ad_roles.py` — Entra group-id → ASOE role mapping
  (`analyst` / `manager` / `admin` / `viewer` / `partner`).
  Configurable per tenant.
* `infra/main.bicep` — Entra App Registration + app role definitions.
* Tests:
  * `tests/test_entra_id_token_validation.py` — JWKS fetch + signature
    verification (recorded JWKS response fixture, no live call on PR
    CI).
  * `tests/test_entra_id_role_mapping.py` — group → role with multiple
    edge cases (no groups → 403; multiple groups → highest role; etc.).
  * Existing HS256 JWT tests stay green under `ASOE_AUTH_MODE=seed`.

**Acceptance criteria:**

* A real Entra ID user in the `analyst` group logs into preprod UI →
  calls `/api/v1/exceptions/resolve` → 200, with token's `org` claim
  matching the tenant_id.
* User not in any mapped group gets 403 with a clear "no role" message.
* Seed-user path still passes the existing test suite.

**Risks:** the `tenant_id` ↔ Entra ID tenant boundary needs a clear
mapping decision (single Entra tenant serving multiple ASOE tenants vs
one Entra tenant per ASOE tenant). Pick the single-tenant story for
preprod; multi-tenant is a GA follow-up.

**Effort:** **L** (3–5 days).

**Dependencies:** Phase 0.

---

## Phase 4 — Key Vault secrets + managed identity (M)

**Goal:** Stop passing `ASOE_JWT_SECRET`, `DATABASE_URL`, LLM provider
keys as plaintext Container App env vars; source them from Key Vault
references.

**Deliverables:**

* `infra/main.bicep`:
  * Provision a Key Vault (RBAC mode).
  * Grant the Container App's managed identity `Key Vault Secrets User`
    role on the Vault.
  * Replace each `@secure()` parameter with a Key Vault secret + a
    Container App env var sourced via `secretref:` pointing at the
    Vault.
* `.github/workflows/deploy-azure.yml` — write secrets into Key Vault
  on first deploy (or reference pre-seeded out-of-band).
* `docs/ops/secrets-rotation.md` — rotation procedure (manual operator
  trigger → Container App revision restart for preprod; sidecar
  hot-reload deferred to GA).

**Acceptance criteria:**

* `az containerapp show -n <app>` exposes no plaintext secret values,
  only `secretref:` pointers.
* Rotating a secret in Key Vault + restarting the Container App
  revision picks up the new value (verified by a forced JWT signing-key
  rotation test).

**Risks:** Container Apps doesn't reload secrets without a revision
restart. For preprod, accept restart-on-rotate; sidecar pattern for GA.

**Effort:** **M** (1–2 days).

**Dependencies:** Phase 0.

---

## Phase 5 — Azure Monitor / Application Insights (M)

**Goal:** Ship metrics + traces + structured logs to Azure Monitor so
the preprod operations surface mirrors the Prometheus + structured-log
view available locally.

**Deliverables:**

* `infra/main.bicep` — enable Container Apps' Prometheus add-on
  scraping `/api/v1/metrics`. Wire to a Log Analytics workspace.
* `asoe2/api/observability/otel.py` — OpenTelemetry FastAPI
  instrumentation + OTLP exporter pointing at the Application Insights
  connection string env var.
* `pyproject.toml` — add an optional extra:
  ```toml
  [project.optional-dependencies]
  azure-otel = [
    "opentelemetry-api>=1.27", "opentelemetry-sdk>=1.27",
    "opentelemetry-instrumentation-fastapi>=0.48",
    "opentelemetry-exporter-otlp>=1.27",
  ]
  ```
* `api/app.py` — initialise OTel only when
  `APPLICATIONINSIGHTS_CONNECTION_STRING` is set (no-op locally; no
  cost on dev).
* `ops/observability/grafana/` — Azure Managed Grafana variant of
  existing dashboards (optional; Log Analytics queries also serve).
* Alerts wired to Action Groups: zero-highlight
  (`anchor_count > 0 ∧ located == 0`), low Layer-2-open rate,
  breaker-OPEN.

**Acceptance criteria:**

* A `/resolve` from the UI produces:
  * One trace in App Insights showing the FastAPI handler → LangGraph
    node → gateway-call chain.
  * Prometheus counters visible in Log Analytics under
    `InsightsMetrics`.
  * Structured logs in Log Analytics with the trace correlation id.

**Risks:** OTel instrumentation can be heavy in LangGraph if not
configured; gate to `INFO` spans for preprod, sample down if needed.

**Effort:** **M** (2–3 days).

**Dependencies:** Phase 0.

---

## Phase 6 — Real platform connectors (XL aggregate; per-sub-phase M–L)

**Goal:** Replace each `StubGateway` in `api/sandbox_gateways.py` /
`api/preprod_gateways.py` with a real connector, one seam at a time.
Order chosen to maximise read-only realism early.

### 6.1 — `email_intake` via Microsoft Graph (L)

* `gateways/email_intake_graph.py` — real connector for `fetch_message`,
  `sender_auth`, `resolve_customer` against Microsoft Graph.
* `duplicate_po_pre_check` + `credit_check` stay stubbed (downstream
  lookups not directly served by Graph).
* Wire via `ASOE_EMAIL_INTAKE_DRIVER=graph` (default = stub).
* Recorded fixtures for the Graph responses (replay on PR CI).
* Circuit-breaker parity is already provided by `GatewayExecutor`.

### 6.2 — `order_extraction` via Azure Document Intelligence (M)

* `extract_order` / `extract_entities` to Document Intelligence custom
  model (or prebuilt invoice/receipt + deterministic post-process).
* Same ADR-045 select-not-generate discipline — never free-generate
  fields, always select from the OCR candidate set.

### 6.3 — SAP S/4HANA OData (L per domain)

Five gateways (`sap_order`, `sap_doc`, `sap_contract`, `sap_block`,
`sap_customer_master`) each get a real connector module + recorded
fixtures. Behaviour-preserving against the StubGateway contracts —
recipes don't change.

### 6.4 — `oms` (M)

Real OMS read connector for `inventory_snapshot`, `matched_po_details`,
`fulfillment_status`, `price_hold_status`.

### 6.5 — `knowledge_graph` / `edi_850` / `change_analysis` (no-op)

These are pure deterministic builders today (`gateways/edi850.py`,
`gateways/knowledge_graph.py`, `recipes/ChangeAnalysisRecipe.py`). No
real-vs-stub gap. Stays as-is.

**Acceptance criteria (per sub-phase):**

* Recorded-fixture replay test passes — red-green path unchanged.
* Nightly `-m live` run against the real Azure-hosted endpoint produces
  the same anchor/field shape as the stub.
* Circuit-breaker trips correctly on a simulated outage; composer
  degrades per ADR-025 / ADR-043 / ADR-045 rules.

**Effort:** **XL** aggregate; each sub-phase is **M–L**.

**Dependencies:** Phase 1.

---

## Phase 7 — Live spatial extraction (M)

**Goal:** Close ADR-045's documented live-path deferral.

**Deliverables:**

* `gateways/document_extraction.py` —
  `AzureDocumentIntelligenceBackend` behind the `propose(...)` seam.
* Recorded fixtures captured from real Document Intelligence runs.
* `tests/eval -m live` gate runnable against the real provider with the
  same `thresholds.yaml`.
* Cost meter (`record_extraction_cost`) wired to actual per-page
  billing.

**Acceptance criteria:**

* Live eval gate meets the same containment / page-accuracy / ECE
  thresholds the replay gate already meets on the golden set.

**Risks:** containment may drop on real scanned documents vs the
born-digital golden set — expand the golden set before final ratify.

**Effort:** **M**.

**Dependencies:** Phase 6.2 (or runs in parallel — separate backend).

---

## Phase 8 — Data governance (deferred per ADR-044) (L)

**Goal:** Close items explicitly out-of-scope this engagement so GA is
unblocked.

**Deliverables:**

* `contracts/policy.py` — per-tenant retention TTL constants.
* A retention sweeper as a Container Apps Job (cron) that calls
  `erase_attachment` on expired records.
* Customer-managed-keys (CMK) on the Storage Account (Key Vault key).
* Erasure tombstone routing into the immutable audit chain (ADR-023).
* `compliance/CODEOWNERS` gate restored on
  `audit_bearing_registry.yaml` and `tests/eval/thresholds.yaml`.
* Encryption-at-rest verification test (both DB and Blob).

**Acceptance criteria:**

* Compliance sign-off recorded for each item.
* GA-readiness gate green (per ADR-044 §6).

**Effort:** **L**.

**Dependencies:** Phase 2 + Phase 4.

---

## Parallelism map

```
Phase 0  ────────────────────────────────────────► (single ship gate)
            │
            ├─► Phase 1 ──► Phase 6.x ──► Phase 7
            │
            ├─► Phase 2  ┐
            │            ├──► Phase 8
            ├─► Phase 4  ┘
            │
            ├─► Phase 3
            │
            └─► Phase 5
```

Phases 1–5 can run in parallel after Phase 0. Phase 6 depends on
Phase 1. Phase 8 depends on Phases 2 + 4.

---

## Phase ↔ Parity matrix

| Capability | Vercel Dev (today) | Azure Pre-Prod (target) | Closing phase |
|---|---|---|---|
| Inbox 9 cases populated | Mock bundles | Sandbox stubs → backend `enrichment_context` | Phase 0 |
| Evidence Detail line items | `MOCK_LINE_ITEMS` | `_project_line_items` from event | Phase 0 |
| Attachment preview + safety bar | Mock bytes + mock anchors | Seed endpoint or real ingestion | Phase 0 (seed) / Phase 6.1 (real Graph) |
| Auth | Seed users + any-password | Entra ID + group → role | Phase 3 |
| Attachment bytes durability | n/a (mock) | Azure Blob (or Postgres BYTEA fallback) | Phase 2 |
| Secrets | n/a (mock) | Key Vault references | Phase 4 |
| Metrics / traces | n/a (mock) | App Insights + Log Analytics | Phase 5 |
| Email source-of-truth | Mock email body | Microsoft Graph | Phase 6.1 |
| SAP data accuracy | Mock | Real S/4HANA OData | Phase 6.3 |
| Spatial overlays | Mock anchors | Live Document Intelligence | Phase 7 |
| Retention / encryption | n/a | Per-tenant TTL + CMK | Phase 8 |

---

## Risk register

| Risk | Mitigation | Owner |
|---|---|---|
| Silent empty registry on production boot → KeyError on first request | Phase 1 fail-loud `register_production_gateways` | Backend |
| Plaintext secrets in Container App env vars | Phase 4 Key Vault references | Platform |
| No Azure AD → ops can't onboard real users | Phase 3 (Entra ID) | Platform + UI |
| Mock-vs-real section drift | Existing architectural locks (asoe2 #176, asoe-ui #195) catch this on PR CI | All |
| Real OCR / SAP outage in preprod | Existing circuit-breaker + composer fallback (ADR-025, ADR-043, ADR-045) | Backend |
| Cold-start migration time on Postgres | Idempotent schema; observe; budget at Phase 0 acceptance | Backend |
| Document Intelligence cost overrun | `EXTRACTION_MAX_COST_USD_PER_PAGE` guardrail + per-page meter already shipped | Backend |
| `tenant_id` ↔ Entra tenant ambiguity | Phase 3 explicit single-tenant story; multi-tenant deferred | Architecture |

---

## Rollback / safety net

Every Phase 1–7 change is **env-flagged**: clearing the env var falls
back to the previous (stub) behaviour. Specifically:

* `ASOE_ENV=sandbox` → original boot path.
* `ASOE_OBJECT_STORE_DRIVER=filesystem` (default) → no Blob driver.
* `ASOE_AUTH_MODE=seed` (default) → original HS256 + seed users.
* Unset `APPLICATIONINSIGHTS_CONNECTION_STRING` → no OTel.
* Unset `ASOE_EMAIL_INTAKE_DRIVER` etc. → real-connector phases revert
  to stubs.

The existing `.github/workflows/deploy-azure.yml` runs a 60-second
health-check after each deploy and rolls back to the previous Container
App revision on failure — so a bad deploy doesn't take preprod down.

Every phase ships with a regression test that fails on its parent
commit (per project test strategy in `CLAUDE.md`).

---

## Open questions for sign-off

1. **Entra ID tenancy model** — single Entra tenant serving multiple
   ASOE tenants, or one Entra tenant per ASOE tenant? (Affects Phase 3
   App Registration + JWKS validation logic.)
2. **Document Intelligence model choice** — Azure prebuilt invoice /
   receipt + post-process, or train a custom model on annotated POs?
   (Affects Phase 6.2 + Phase 7 cost ceiling.)
3. **SAP environment for preprod** — sandbox SAP or a real S/4HANA
   tenant? (Affects Phase 6.3 connector + fixture capture.)
4. **Key Vault rotation cadence** — manual operator trigger acceptable
   for preprod? (Affects Phase 4 sidecar / hot-reload work.)
5. **Application Insights workspace** — shared with other Azure
   projects, or dedicated for ASOE? (Affects Phase 5 alert routing +
   cost.)
6. **Attachment-byte residency** — does any tenant require regional
   pinning (Azure Blob geo) for compliance? (Affects Phase 2 +
   Phase 8.)

---

## Out-of-scope (this plan)

* **GA-only items** beyond Phase 8 (multi-tenant Entra, customer-managed
  HSMs, full audit-chain integration with the ADR-023 immutable log).
* **LLM provider migration** — ADR-039 / ADR-040 cover the LLM
  routing story; orthogonal to deployment parity.
* **Web/mobile distribution** — only Container App URL parity is in
  scope (no public marketing pages, no app-store distribution).

---

## Suggested first ticket-set

1. **PARITY-0** Phase 0 — deploy to Azure with sandbox stubs (S).
2. **PARITY-1** Phase 1 — preprod gateway registration + fail-loud
   production boot (M).
3. **PARITY-2** Phase 2 — Azure Blob driver (M).
4. **PARITY-3** Phase 3 — Entra ID auth, backend half (M then L).
5. **PARITY-4** Phase 4 — Key Vault references (M).
6. **PARITY-5** Phase 5 — App Insights + OTel (M).
7. **PARITY-6.x** Phase 6 sub-tickets — one per gateway domain.
8. **PARITY-7** Phase 7 — live Document Intelligence (M).
9. **PARITY-8** Phase 8 — governance items (L).

Each ticket lands as a self-contained PR with the regression test that
fails on its parent commit. The plan is deliberately scoped so any
phase can be deferred without blocking the others (modulo the
`Phase 0 → all` and `Phase 1 → Phase 6` dependencies).
