# tasks.md
## Project: CPG Agentic Pricing Exception System
## Phase-Based Incremental Build Plan
---
## PHASE 0 — Foundation (NO BUSINESS LOGIC)
### 0.1 Repository Structure
- [x] Create folders:
  - `contracts/`
  - `skills/`
  - `recipes/`
  - `orchestration/`
  - `compliance/`
  - `constraints/`
  - ~~`mcp/`~~ (deferred — MCP integration is stubbed per architecture_v3.md §4; directory not created)
  - `tests/`
  - `prompts/`
  - `llm/` (added during Phase 9 — Outlines model loader for `OutlinesConstrainedBackend`)
- [x] Move existing recipes into `recipes/`
- [x] Move SKILL.md into `skills/`
✅ Outcome: clean separation of brain vs muscle
---
### 0.2 Type Contracts
- [x] Define Pydantic models for:
  - OrderEvent
  - PricingDiscrepancy
  - RecipeInvocation
  - ComplianceDecision
  - ExecutionLog
  - GraphState
- [x] Define constrained output schemas for Guidance / Outlines:
  - IntentDecision
  - ShadowDecision
  - RecipeProposal
- [x] Enforce strict validation before execution
✅ Outcome: no untyped execution paths
---
## PHASE 1 — Skill Loading & Reasoning
### 1.1 Skill Loader
- [x] Implement dynamic loader for `skills/*.md`
- [x] Load skills **only when relevant**
- [x] Ensure skill text is injected verbatim (no summarization)
✅ Outcome: progressive disclosure works as designed
---
### 1.2 Intent Classifier (Non-Executing)
- [x] Given an OrderEvent:
  - Identify price gap
  - Classify intent:
  - `CONTRACTUAL_CORRECTION`
  - `CREDIT_BLOCK`
  - `MASS_PRICING_ERROR`
  - `DUPLICATE_PO`
- [x] Constrain intent output vocabulary using Guidance / Outlines
- [x] Output intent + confidence
- [x] NO recipe calls yet
✅ Outcome: reasoning-only stage is testable
---
## PHASE 2 — Compliance Shadow
### 2.1 Shadow Interface
- [x] Define ComplianceShadow API contract
- [x] Implement stub that returns:
  - `GREEN`
  - `YELLOW`
  - `RED`
- [x] Constrain shadow verdict using Guidance / Outlines
- [x] Log shadow decision with TraceID
- [x] Include `TraceID`, reasons, policy hits
✅ Outcome: compliance is always first-class
---
### 2.2 Shadow Enforcement
- [x] Block execution on `RED`
- [x] Force explanation output on `RED`
- [x] Route `YELLOW` to `MANUAL_REVIEW_REQUIRED`
- [x] Allow auto-proceed only on `GREEN`
✅ Outcome: no silent violations
---
## PHASE 3 — Recipe Invocation
### 3.1 Recipe Registry
- [x] Register recipes with:
  - Name
  - Required parameters
  - Allowed intents
- [x] Reject unknown recipe calls
✅ Outcome: zero dynamic execution
---
### 3.2 Deterministic Execution Wrapper
- [x] Execute recipe via subprocess or function call
- [x] Constrain recipe proposal to registered recipe names using Guidance / Outlines
- [x] Capture:
  - Inputs
  - Outputs
  - Errors
- [x] Return immutable execution log
✅ Outcome: audit-ready execution
---
## PHASE 4 — Orchestration (LangGraph)
### 4.1 State Machine Definition
States:
- `Ingest`
- `Classify`
- `Load Skill`
- `Shadow Audit`
- `Validate Types`
- `Execute Recipe`
- `Fail to Human`
- `Complete`
✅ Outcome: predictable, loop-safe behavior
### 4.2 Circuit Breaker
- [x] Track execution counts
- [x] Enforce:
  - Max updates / window e.g. max 50 pricing updates / 5-minute window
  - Max financial exposure e.g. max $10,000 total dollar variance per batch
- [x] Route to HITL / `FAIL_TO_HUMAN` on breach
✅ Outcome: systemic risk control
---
## PHASE 5 — Observability & Tests
### 5.1 LangFuse-Ready Integration/Tracing
- [x] Trace:
  - Skill used
  - intent selected
  - Shadow verdict
  - Recipe output
  - RAG chunks (reserved for V2 — empty in V1.0)
  - TraceID
- [x] Keep implementation self-host ready
### 5.2 Golden Tests
- [x] Test each intent → recipe mapping
- [x] Test shadow rejection paths
- [x] Test FAIL_TO_HUMAN paths
- [x] Test constrained output schemas and allowed vocabularies
✅ Outcome: regression-proof system
---
## PHASE 6 — Hardening
- [x] Kill switch config
- [x] Read-only “explain mode”
- [x] Documentation for auditors
- [x] Document Guidance / Outlines safeguards for downstream systems
✅ Outcome: production readiness
---
## PHASE 7 — Infrastructure Gateways & Multi-Step Workflows
### 7.1 Infrastructure Gateway Layer (Hexagonal Architecture)
- [x] Define `InfrastructureGateway` protocol (Port) in `gateways/base.py`
- [x] Implement Gateway Registry (`register_gateway`, `get_gateway`, `clear_registry`)
- [x] Implement `GatewayExecutor` with structured tracing and error handling
- [x] Implement `StubGateway` test double (canned responses, call recording)
- [x] Add typed contracts: `GatewayRequest`, `GatewayResponse`, `GatewayDependency`, `GatewayEffect`
- [x] Extend `RecipeSpec` with optional `dependencies` and `effects` tuples
- [x] Add `resolve_dependencies` node (pre-recipe gateway data resolution)
- [x] Add `apply_effects` node (post-recipe gateway side effect application)
- [x] Wire new nodes into graph: `validate_types → resolve_dependencies → execute_recipe → apply_effects → END`
- [x] Add `gateway_calls` field to `TraceRecord` for observability
✅ Outcome: recipes stay pure; infrastructure I/O is decoupled via Ports & Adapters

### 7.2 Multi-Step Workflow Runner (Saga Pattern)
- [x] Define typed contracts: `WorkflowStep`, `WorkflowDefinition`, `WorkflowStepResult`, `WorkflowResult`
- [x] Implement `WorkflowRunner.run()` — sequential step execution through full graph
- [x] Implement Saga compensation — LIFO reverse through completed steps on failure
- [x] Support `input_mapping` — carry state forward between steps
- [x] `WorkflowResult.status`: `COMPLETE`, `FAILED`, `COMPENSATED`, `PARTIAL`
- [x] Each step runs through full compliance shadow independently
✅ Outcome: multi-intent workflows with compensation; each step fully audited

### 7.3 DUPLICATE_PO Fallback Backend
- [x] Add `DUPLICATE_PO` classification branch in `DeterministicFallbackBackend.classify_intent()`
- [x] Add `DUPLICATE_PO → DuplicatePORecipe.py` mapping in `propose_recipe()`
✅ Outcome: DUPLICATE_PO intent is fully routable end-to-end in CI/test mode
---
## PHASE 8 — Local Execution Sandbox
### 8.1 SQLite Seeder (`tests/sandbox/seed.py`)
- [x] Define SQLite schema: `customers`, `distribution_centers`, `promotions`, `sap_pricing`, `retailer_contracts`, `credit_profiles`, `edi_events`
- [x] Seed 10 customers, 5 DCs, 4 promotions, 10 SKUs, 15 retailer contracts, 8 credit profiles
- [x] Seed 18 EDI events covering all four intents (CONTRACTUAL_CORRECTION ×7, CREDIT_BLOCK ×4, MASS_PRICING_ERROR ×3, DUPLICATE_PO ×4)
- [x] CLI flags: `--db <path>` and `--reset`
- [x] `load_events()` helper used by the Streamlit UI
✅ Outcome: repeatable, deterministic fixture data for local exploration

### 8.2 Streamlit Execution-Trace Visualiser (`tests/sandbox/ui/app.py`)
- [x] Sidebar: event picker from SQLite or custom form input
- [x] Run event through full `run_graph()` pipeline
- [x] Header metrics: intent, shadow verdict (with colour), recipe, final status
- [x] Step-by-step execution trace panel (9 nodes)
- [x] Compliance Shadow detail (reasons, policy hits)
- [x] Explanation / SKILL.md viewer / Prompt Preview expander
- [x] Full JSON trace (GraphState dump)
- [x] Gateway activity panel (resolved dependencies + effect results)
- [x] Environment info banner (backend, explain mode, kill switch, DB path)
✅ Outcome: engineers and stakeholders can explore the pipeline interactively without writing code

### 8.3 LocalHFBackend (`tests/sandbox/llm/local_backend.py`)
- [x] Implements same interface as `OutlinesConstrainedBackend` (classify_intent, propose_recipe, shadow_decision)
- [x] Uses Outlines constrained-JSON generation with a local HuggingFace model
- [x] Graceful fallback to `DeterministicFallbackBackend` on load failure (missing deps, no weights)
- [x] Injected via `LOCAL_LLM_BACKEND_CLASS` env var — no code changes required
- [x] Prompt builders: `intent_prompt`, `recipe_prompt`, `shadow_prompt`
✅ Outcome: sandbox can use a real constrained-generation model without cloud dependency

### 8.4 Prompt Templates (`tests/sandbox/llm/prompts.py`)
- [x] Standalone `intent_prompt()`, `recipe_prompt()`, `shadow_prompt()` from raw event dicts
- [x] Used by the UI "Prompt Preview" expander to show what the LLM would receive
✅ Outcome: prompt transparency for demos and audits

### 8.6 Headless CLI Runner (`tests/sandbox/cli.py`)
- [x] Run all seeded events or filter by `--event` / `--intent`
- [x] Per-event execution trace: intent, shadow verdict, recipe, final status, gateway activity
- [x] `--json` flag for full GraphState JSON dump
- [x] `--prompts` flag for LLM prompt previews (intent, recipe, shadow)
- [x] `--quiet` flag for summary-only output
- [x] Colour-coded summary table with pass/fail/error counts
- [x] Honours `ASOE_EXPLAIN_MODE`, `ASOE_KILL_SWITCH`, `LOCAL_LLM_BACKEND_CLASS`
- [x] No Streamlit dependency — uses only core modules + sandbox seed
✅ Outcome: engineers can run sandbox scenarios from the terminal without a browser

### 8.7 Sandbox Dependencies (`tests/sandbox/requirements-sandbox.txt`)
- [x] `streamlit>=1.35.0` (required only for UI, not CLI runner)
- [x] `outlines`, `transformers`, `torch`, `accelerate`, `huggingface-hub` (all optional)
- [x] Core production deps not duplicated
✅ Outcome: sandbox installs are isolated from CI and production requirements
---
## PHASE 9 — Containerized Deployment
### 9.1 Dockerfiles (3-Container Architecture)
- [x] `Dockerfile.core` — core orchestration engine (LangGraph + recipes + Compliance Shadow)
- [x] `Dockerfile.ui` — Streamlit sandbox UI (core + streamlit, no GPU deps)
- [x] `Dockerfile.inference` — local LLM inference (Outlines + torch + transformers for Compliance Shadow)
- [x] `.dockerignore` — excludes .git, __pycache__, sandbox.db, k8s/
- [x] Non-root user (`asoe`, UID 1000) in all images
- [x] `uv` for fast, deterministic dependency resolution (per architecture_v3.md §4)
✅ Outcome: each container installs only its required dependency group

### 9.2 Docker Compose (Local Development)
- [x] `docker-compose.yml` — core + ui services (always on), inference service (optional `--profile inference`)
- [x] `.env.example` — documents all runtime env vars
- [x] Shared env block (`x-core-env`) for ASOE_KILL_SWITCH, ASOE_EXPLAIN_MODE
- [x] Source-mount volumes for hot-reload during development
- [x] `hf-model-cache` volume persists model weights across rebuilds
- [x] Health checks for all services
✅ Outcome: `docker compose up` runs the full stack locally

### 9.3 Kubernetes Manifests (AKS Production)
- [x] `k8s/namespace.yaml` — `asoe` namespace with compliance label
- [x] `k8s/core/` — Deployment (2 replicas, topology spread), Service (ClusterIP), ConfigMap
- [x] `k8s/ui/` — Deployment (2 replicas), Service (ClusterIP behind APIM)
- [x] `k8s/inference/` — Deployment (1 replica, Intel AMX nodeSelector, 20 Gi memory), Service (ClusterIP)
- [x] Azure Workload Identity annotations on all pod templates
- [x] Non-root security context on all pods
✅ Outcome: deployment manifests align with architecture_v3.md §4 infrastructure stack
---
## PHASE 10 — LangFuse Observability Integration
Build prompt: `prompts/phase_10_langfuse.md`
### 10.1 LangFuse Sink (`observability/langfuse_sink.py`)
- [x] Implement lazy-init LangFuse client (env-var driven: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`)
- [x] `forward()` — maps `TraceRecord` to LangFuse trace + spans (classify, load_skill, shadow_audit, execute_recipe) + terminal_status score
- [x] `flush()` — explicit flush for short-lived processes (CLI runner)
- [x] `reset_client()` — test helper for re-initialisation
- [x] Failure isolation: all LangFuse errors caught; stdlib logging remains authoritative
- [x] SDK compatibility: auto-detects langfuse v2 (trace/span/score) vs v4+ (start_observation/create_score)
- [x] Verified end-to-end against self-hosted LangFuse v2.95.1 (19 traces with spans + scores confirmed via API)
✅ Outcome: optional LangFuse forwarding with zero impact on existing behaviour

### 10.2 Tracer Integration
- [x] `Tracer.emit()` calls `langfuse_sink.forward()` after stdlib logging
- [x] Import is lazy (inside `emit()`); no module-level langfuse dependency
- [x] Exception isolation: forward failure logged at DEBUG, never blocks
✅ Outcome: dual-emit (stdlib + LangFuse) with backward compatibility

### 10.3 Dependency & Configuration
- [x] `pyproject.toml` — `langfuse` added as optional dependency group
- [x] `Dockerfile.core` and `Dockerfile.ui` — `langfuse>=2.0.0` in pip install
- [x] `Dockerfile.inference` — no change (no observability module)
- [x] `docker-compose.yml` — `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` in shared `x-core-env`
- [x] `.env.example` — LangFuse env vars documented
- [x] `k8s/core/secret-provider.yaml` — `langfuse-public-key` and `langfuse-secret-key` synced from Azure Key Vault
✅ Outcome: LangFuse keys managed via Key Vault CSI in production, env vars in dev

### 10.4 Sandbox Support
- [x] CLI runner (`tests/sandbox/cli.py`) — `--langfuse-flush` flag, LangFuse status in env banner
- [x] Streamlit UI (`tests/sandbox/ui/app.py`) — LangFuse status in environment expander
- [x] `requirements-sandbox.txt` — `langfuse>=2.0.0` as commented optional dep
✅ Outcome: sandbox tools forward traces to LangFuse when configured

### 10.5 Tests
- [x] 15 new tests in `tests/test_observability.py` (540 total)
- [x] `TestLangFuseSinkDisabled` — no-op when keys missing, package missing, keys empty
- [x] `TestLangFuseSinkWithMockClient` — trace/span/score creation, level mapping, exception isolation
- [x] `TestTracerEmitWithLangFuse` — dual-emit, sink failure does not block stdlib
- [x] All tests network-free (mock client injection, no live LangFuse connection)
✅ Outcome: full coverage without CI network dependency

### 10.6 Documentation
- [x] `DESIGN.md` §9 — LangFuse forwarding table, env var reference, container contents
- [x] `README.md` — LangFuse section (install, configure, test commands, sandbox usage, Docker)
- [x] `docs/AUDITOR_GUIDE.md` — audit trail section updated with LangFuse forwarding
- [x] `tasks.md` — this phase checklist
✅ Outcome: docs cover setup, testing, and production deployment

### 10.7 Azure Container Apps deployment + full-topology span coverage
- [x] `infra/main.bicep` — `langfuseHost`/`langfusePublicKey`/`langfuseSecretKey` parameters; two new Container App secrets; three env vars wired onto the API container (sink stays no-op when keys are empty)
- [x] `scripts/deploy-azure.sh` — preserves the LangFuse key pair across re-runs; honours `LANGFUSE_HOST` env override (US Hobby default, EU / self-hosted on demand)
- [x] `scripts/set-secrets.sh` — paired key rotation; refuses single-key updates
- [x] `observability/langfuse_sink.py` — span coverage expanded from 4 → all 11 graph nodes (`ingest`, `classify`, `load_skill`, `validate_circuit_breaker`, `select_recipe`, `resolve_dependencies`, `validate_types`, `shadow_audit`, `execute_recipe`, `apply_effects`, `build_analysis`); LLM `generation` observations attached as children of their owning step span (intent → classify, recipe → select_recipe, shadow → shadow_audit) instead of trace-root siblings; classify / select_recipe / shadow_audit metadata records `backend_used = "<provider>:<model_id>"` (or `"deterministic"`) so degraded runs are visible at a glance
- [x] 71/71 tests in `tests/test_observability.py` (3 new — full-topology span set, YELLOW shadow suppression of execute_recipe, backend_used metadata)
- [x] `docs/deploy-azure-container-apps.md` + `infra/README.md` — operator runbook for enable / rotate / region override
✅ Outcome: LangFuse Cloud (Hobby) is wired into pre-prod Azure deploy; every graph node renders in the LangFuse UI with its LLM call nested inline
---
## PHASE 11 — Duplicate PO Product Spec Gap Closure
Build prompt: `docs/specs/duplicate-po-product-spec.md`
### 11.1 Resolution Actions (Phase A)
- [x] Extend resolution actions to all 6 spec-defined types: BLOCK_AND_NOTIFY, MERGE, SUPERSEDE, ALLOW_BOTH, ESCALATE, REQUEST_BUYER_CONFIRMATION
- [x] Add `AllowedResolutionAction` constrained vocabulary in `constraints/specs.py`
- [x] Sync across guidance_backend.py regex, fallback_backend.py, recipe default mapping
- [x] Replace ANNOTATE_AND_PASS/ALLOW with spec-aligned actions
✅ Outcome: all 6 resolution actions from spec §3.1 available and constrained

### 11.2 Gateway Dependencies for Resolution Context (Phase B)
- [x] Declare OMS gateway dependencies on DuplicatePORecipe (get_fulfillment_status, get_matched_po_details)
- [x] Inject resolution context (original_fulfilled, has_revision_indicator, line_items_identical) into recipe params via validate_types
- [x] Recipe signature accepts optional resolution context (defaults to None)
✅ Outcome: recipes receive pre-resolved context without I/O; gateway layer decouples data access

### 11.3 Resolution Decision Tree (Phase C)
- [x] Implement `_resolve_action()` decision tree per spec §3.2
- [x] AUTO_BLOCK tier: BLOCK_AND_NOTIFY / ALLOW_BOTH / SUPERSEDE / MERGE based on context
- [x] REVIEW_REQUIRED tier: SUPERSEDE / ESCALATE / REQUEST_BUYER_CONFIRMATION based on context
- [x] SOFT_FLAG / PASS: use default actions (low confidence)
- [x] Fallback to defaults when no gateway context available
✅ Outcome: agent recommends specific action based on duplicate type, not just classification

### 11.4 Autonomy-Level Policy Mapping (Phase D)
- [x] Add DUPLICATE_PO_AUTONOMY_LEVELS to contracts/policy.py (action → L1–L4)
- [x] Recipe output includes autonomy_level field
- [x] execute_recipe node routes L1/L2 to MANUAL_REVIEW_REQUIRED, L3/L4 auto-execute
✅ Outcome: human approval required for MERGE/SUPERSEDE/REQUEST_BUYER_CONFIRMATION; auto-execute for BLOCK_AND_NOTIFY/ALLOW_BOTH

### 11.5 Override Audit Fields (Phase E)
- [x] Add resolved_by, resolved_action, resolution_notes to ExecutionLog
- [x] Add corresponding fields to TraceRecord for audit trail
- [x] Tracer.build_record() extracts override fields from execution log
✅ Outcome: human overrides captured in compliance audit trail (SOX requirement)

### 11.6 Buyer Notification Gateway Effect (Phase G)
- [x] Recipe output includes notification_template per action (duplicate_po_blocked, duplicate_po_inquiry, duplicate_po_amended, or None)
- [x] DuplicatePORecipe registry declares buyer_notification GatewayEffect
- [x] apply_effects node dispatches notification after recipe execution
✅ Outcome: buyer communication handled via existing gateway effect pattern

### 11.7 Tests
- [x] 44 new tests (584 total): decision tree (11), autonomy routing (8), notification templates (6), override audit (7), gateway deps (4), registry (4), constraint vocab (4)
✅ Outcome: all new code paths covered at recipe, node, graph, and integration levels
---
## PHASE 12 — FastAPI API Layer
Build prompt: architecture_v3.md §8, §11.1–11.3
### 12.1 API Application Structure
- [x] Create `api/` module with FastAPI application factory (`api/app.py`)
- [x] Add standard error envelope (`api/errors.py`) per architecture_v3.md §8.3
- [x] Add request/response Pydantic models (`api/schemas.py`)
- [x] Add in-memory exception store (`api/store.py`) — V1 persistence (PostgreSQL in V1.1)
✅ Outcome: structured API module with typed contracts

### 12.2 Authentication & RBAC
- [x] Implement JWT extraction and validation (`api/deps.py`) — HS256 dev stub, Key Vault in production
- [x] Implement RBAC dependency factory (`require_role()`) — 5 roles per architecture_v3.md §11.2
- [x] Implement tenant extraction from JWT `org` claim (`get_tenant_id()`)
- [x] Application-layer tenant isolation on all queries
✅ Outcome: JWT auth + RBAC + tenant scoping enforced on all protected endpoints

### 12.3 REST Endpoints (19 routes)
- [x] `GET /api/v1/health` — public health check with dynamic enum serving (Guardrail #2)
- [x] `POST /api/v1/exceptions/resolve` — synchronous resolution via `run_graph()`
- [x] `POST /api/v1/exceptions/resolve/async` — async resolution (V1 stub, runs synchronously)
- [x] `POST /api/v1/exceptions/resolve/explain` — explain mode dry-run
- [x] `GET /api/v1/exceptions` — paginated exception queue (filter by status, intent)
- [x] `GET /api/v1/exceptions/stats` — dashboard metrics
- [x] `GET /api/v1/exceptions/{id}` — exception detail
- [x] `GET /api/v1/exceptions/{id}/trace` — full TraceRecord JSON
- [x] `PATCH /api/v1/exceptions/{id}/override` — human override (manager+)
- [x] `POST /api/v1/exceptions/{id}/approve` — resume paused exception (manager+)
- [x] `POST /api/v1/exceptions/{id}/reject` — reject paused exception (manager+)
- [x] `POST /api/v1/workflows` — multi-step workflow via `WorkflowRunner`
- [x] `PUT /api/v1/policies/{tenant_id}` — policy override update (admin only)
- [x] `POST /api/auth/login` — email/password auth (MFA enforced)
- [x] `POST /api/auth/sso/init` — SSO initiation (stub)
- [x] `GET /api/auth/sso/callback` — SSO callback (stub)
- [x] `POST /api/auth/mfa/verify` — MFA verification (stub)
- [x] `POST /api/auth/refresh` — token refresh
- [x] `GET /api/auth/me` — current user profile
✅ Outcome: all architecture_v3.md §8.2 endpoints implemented with auth + tenant isolation

### 12.4 Sandbox Updates
- [x] CLI runner (`tests/sandbox/cli.py`) — API server availability in environment banner
- [x] Streamlit UI (`tests/sandbox/ui/app.py`) — API server availability in environment expander
✅ Outcome: sandbox tools surface API server status

### 12.5 Tests
- [x] 42 new tests in `tests/test_api.py` (659 total): health (2), auth/JWT (3), RBAC (8), tenant isolation (2), resolve (5), CRUD (6), override (1), approve/reject (3), workflows (2), policies (1), auth endpoints (7), error envelope (2)
✅ Outcome: full coverage of API endpoints, auth, RBAC, tenant isolation, and error handling

### 12.6 Documentation
- [x] `DESIGN.md` §15 — API layer module map, endpoint table, auth/RBAC docs
- [x] `DESIGN.md` §17 — test_api.py added to test coverage table
- [x] `tasks.md` — this phase checklist
✅ Outcome: docs cover API structure, endpoints, auth, and testing
---
## PHASE 13 — Database Layer (PostgreSQL Schema & Migrations)
Build prompt: architecture_v3.md §9.2, §9.1, §11.3
### 13.1 PostgreSQL Migration SQL
- [x] `db/migrations/V001__initial_schema.sql` — full PostgreSQL schema per architecture_v3.md §9.2
- [x] 5 tables: `exceptions`, `traces`, `policy_overrides`, `policy_audit_log`, `checkpoints`
- [x] Indexes: tenant+state, trace_id, tenant+order, audit tenant, pending checkpoints
- [x] `context_embedding VECTOR(1536)` column (pgvector V2 readiness, not indexed)
- [x] Intent CHECK constraint matching `AllowedIntent` enum
- [x] SOX immutability trigger on `policy_audit_log` (prevents UPDATE/DELETE)
- [x] Row-Level Security policies on `exceptions`, `traces`, `policy_overrides`, `checkpoints`
- [x] RLS misconfiguration guard: `current_setting('app.current_tenant_id', true) IS NOT NULL`
- [x] `schema_migrations` version tracking table
✅ Outcome: production-ready PostgreSQL schema with RLS, SOX triggers, and pgvector

### 13.2 Migration Runner
- [x] `db/migrations/runner.py` — auto-detects PostgreSQL vs SQLite
- [x] SQLite-compatible subset schema for CI testing (no extensions, RLS, triggers, or VECTOR)
- [x] Idempotent execution (tracks applied versions in `schema_migrations`)
- [x] CLI entrypoint: `DATABASE_URL=... python -m db.migrations.runner`
✅ Outcome: one command applies schema to any supported backend

### 13.3 Connection Adapters
- [x] `db/connection.py` — `SQLiteAdapter` (stdlib) + `PostgresAdapter` (psycopg2/psycopg)
- [x] `create_adapter()` factory auto-detects from DATABASE_URL
- [x] PostgresAdapter sets `app.current_tenant_id` session var for RLS enforcement
- [x] Thread-local connections for SQLite, per-request for PostgreSQL
✅ Outcome: single interface for both backends; RLS tenant context propagated

### 13.4 Repository Layer
- [x] `ExceptionRepository` — create, get, list (paginated + filtered), update, stats
- [x] `TraceRepository` — create, get_by_exception
- [x] `PolicyRepository` — create_override (with automatic audit log), get_override, list_audit_log
- [x] All queries include `tenant_id` predicate (application-layer isolation)
- [x] JSON serialization/deserialization for `resolution_data`, `trace_record`, `value` fields
✅ Outcome: typed repository layer with tenant isolation and SOX audit trail

### 13.5 API Integration
- [x] `DatabaseBackedStore` in `api/store.py` — same interface as `ExceptionStore`
- [x] Module-level singleton auto-selects backend: `DATABASE_URL` set → DB, unset → in-memory
- [x] API routes work unchanged regardless of backend
✅ Outcome: zero-change API upgrade path from in-memory to PostgreSQL

### 13.6 Docker Compose
- [x] Added `postgres` service (pgvector/pgvector:pg16) with healthcheck
- [x] Added `redis` service (redis:7-alpine) with healthcheck
- [x] Core service depends on postgres + redis health
- [x] `DATABASE_URL` and `REDIS_URL` in shared `x-core-env` block
- [x] `pgdata` and `redisdata` volumes for persistence
- [x] `.env.example` updated with database/redis variables
✅ Outcome: `docker compose up` provisions full stack including PostgreSQL + Redis

### 13.7 Tests
- [x] 31 new tests in `tests/test_db.py` (690 total): schema (5), exception CRUD (13), trace (2), policy+audit (4), DatabaseBackedStore (7)
✅ Outcome: full repository coverage using SQLite in-memory; no PostgreSQL required for CI

### 13.8 PostgreSQL Integration Tests
- [x] 35 new tests in `tests/test_postgres.py` exercising real PostgreSQL (1007 total)
- [x] `_QmarkCursorWrapper` in `db/connection.py`: translates `?`→`%s` for PostgreSQL compatibility
- [x] Schema migration V001 on real PostgreSQL (pgcrypto, pgvector, UUID, JSONB, TIMESTAMPTZ)
- [x] Row-Level Security: tenant isolation enforced at database level
- [x] SOX immutability trigger: UPDATE/DELETE blocked on `policy_audit_log`
- [x] Repository CRUD on PostgreSQL: exceptions, traces, policies
- [x] DatabaseBackedStore full round-trip on PostgreSQL
- [x] UNIQUE index on `exceptions.trace_id` (required for checkpoints FK)
- [x] Tests auto-skip when PostgreSQL unavailable (`ASOE_TEST_POSTGRES_URL`)
✅ Outcome: PostgreSQL-specific features (RLS, SOX trigger, JSONB, UUID) are test-covered

### 13.9 Documentation
- [x] `DESIGN.md` §16 — database layer docs (schema, RLS, adapters, repositories)
- [x] `DESIGN.md` §1 — db/ module added to module structure
- [x] `tasks.md` — this phase checklist
✅ Outcome: docs cover schema, migration, adapters, and repository layer
---
## PHASE 14 — Auth & Security Hardening
Build prompt: architecture_v3.md §11.1–11.6
### 14.1 Token Expiry & Types (§11.1)
- [x] Access tokens: 15-minute expiry with `exp` and `iat` claims
- [x] Refresh tokens: 7-day expiry with `token_type: "refresh"` claim
- [x] `_jwt_decode()` validates `exp` claim — expired tokens return 401
- [x] Refresh endpoint validates `token_type == "refresh"` — rejects access tokens
- [x] Refresh rotation: issues new access + new refresh token on each refresh
- [x] `auth_method` claim: `"password+mfa"` for login flow, `"sso"` for SSO flow
✅ Outcome: JWT lifecycle matches §11.1 specification

### 14.2 Environment Isolation (§11.6)
- [x] JWT `env` claim validated against `ASOE_ENV` env var on every authenticated request
- [x] Mismatch returns 403 with generic "Access denied." — no internal state leaked
- [x] Sandbox token → production server blocked before business logic executes
✅ Outcome: cross-environment credential use prevented at API boundary

### 14.3 X-Trace-ID Propagation (§11.4)
- [x] `TraceIDMiddleware` in `api/middleware.py` — extracts or generates UUID
- [x] Client-provided `X-Trace-ID` propagated unchanged
- [x] Missing header → UUID generated at API boundary
- [x] Trace ID stored in `request.state.trace_id` and returned in every response
- [x] Available to resolve endpoints for graph execution correlation
✅ Outcome: end-to-end trace correlation from API → graph → TraceRecord

### 14.4 Partner-Role Scoping (§11.3)
- [x] `AuthenticatedUser` includes `retailer_id` field from JWT claim
- [x] `create_access_token()` accepts `retailer_id` parameter
- [x] Partner users filtered to own orders in list endpoint
- [x] Partner users blocked from resolve, override, approve, reject, trace endpoints
✅ Outcome: partner-role isolation enforced at application layer

### 14.5 Configurable JWT Secret (§11.5)
- [x] `_get_jwt_secret()` reads from `ASOE_JWT_SECRET` env var
- [x] Dev fallback when env var unset
- [x] Tokens signed with wrong secret rejected (401)
✅ Outcome: production deployments use Key Vault-managed secret via env var

### 14.6 Tests
- [x] 28 new tests in `tests/test_security.py` (718 total): token expiry (4), token types (4), auth_method (2), trace_id (4), JWT secret (3), env isolation (3), partner scoping (5), error security (2), plus 1 updated test in test_api.py
✅ Outcome: full coverage of all §11 security requirements

### 14.7 Documentation
- [x] `DESIGN.md` §15.2 — updated auth docs with token types, expiry, env isolation, middleware
- [x] `tasks.md` — this phase checklist
✅ Outcome: security docs match implementation
---
## PHASE 15 — WebSocket / Redis Real-Time Event Publishing
Build prompt: architecture_v3.md §10, §9.3
### 15.1 Event Schemas (`api/events.py`)
- [x] `WSEvent` envelope: type, trace_id, exception_id, tenant_id, timestamp, payload
- [x] 4 event types: `pipeline_progress`, `exception_update`, `task_complete`, `error`
- [x] Typed payload models: `PipelineProgressPayload`, `ExceptionUpdatePayload`, `TaskCompletePayload`, `ErrorPayload`
- [x] Factory class methods: `WSEvent.pipeline_progress()`, `.exception_update()`, `.task_complete()`, `.error()`
- [x] `to_json()` for Redis serialization
✅ Outcome: typed event contract matching architecture_v3.md §10.2

### 15.2 Pub/Sub Manager (`api/pubsub.py`)
- [x] `InMemoryPubSub`: publish, get_recent, get_replay (timestamp-based), clear — for testing/dev
- [x] `RedisPubSub`: publish to `asoe:ws:{tenant_id}`, sorted-set replay buffer (60s TTL), subscribe
- [x] `create_pubsub()` factory: `REDIS_URL` set → Redis, unset → in-memory
- [x] Publish failures logged at WARNING, never block (§9.3 partial failure recovery)
- [x] Module-level `event_publisher` singleton
✅ Outcome: dual-backend pub/sub with graceful degradation

### 15.3 WebSocket Hub (`api/routes/ws.py`)
- [x] `ws://host/api/v1/ws` endpoint mounted in `api/app.py`
- [x] Auth protocol: first message `{ "type": "auth", "token": "eyJ..." }` — JWT validated, tenant extracted
- [x] Replay: `last_seen` timestamp triggers 60s buffer replay
- [x] In-memory mode: ping/pong polling for new events
- [x] Redis mode: subscribe to `asoe:ws:{tenant_id}` channel, forward events
- [x] Tenant isolation: client receives events only for their tenant
- [x] Auth failure: returns error message, closes with code 4001
✅ Outcome: authenticated, tenant-scoped real-time event streaming

### 15.4 Resolve Endpoint Integration
- [x] All 3 resolve endpoints (sync, async, explain) publish `task_complete` event
- [x] `_publish_task_complete()` helper publishes to `event_publisher`
- [x] Events include trace_id, exception_id, tenant_id, final_status, explanation
✅ Outcome: every graph execution is observable in real-time

### 15.5 Tests
- [x] 21 new tests in `tests/test_websocket.py` (739 total): event schemas (6), InMemoryPubSub (6), resolve publishing (3), WebSocket auth/streaming (6)
✅ Outcome: full coverage of event schemas, pub/sub, resolve integration, and WebSocket protocol

### 15.6 Documentation
- [x] `DESIGN.md` §17 — WebSocket/Redis event publishing docs
- [x] `prompts/phase_15_websocket_redis.md` — build prompt
- [x] `tasks.md` — this phase checklist
✅ Outcome: real-time event publishing documented and reproducible
---
## PHASE 16 — V1 Foundation Guardrail Tests (CI Enforcement)
Build prompt: architecture_v3.md §15
### 16.1 Guardrail #1 — No intent-specific logic in pipeline nodes
- [x] AST inspection of orchestration/nodes.py: no Compare nodes test against intent string literals
- [x] Grep check: no quoted intent strings in nodes.py
- [x] Sanity check: all expected pipeline functions exist
✅ Outcome: CI fails if anyone adds `if intent == "DUPLICATE_PO"` to a node

### 16.2 Guardrail #2 — Dynamic enum serving
- [x] Health endpoint serves `allowed_intents` matching `AllowedIntent.__args__`
- [x] Health endpoint serves `allowed_recipes` matching `AllowedRecipeName.__args__`
- [x] 11 lifecycle states per architecture_v3.md §9.1
- [x] Route imports from `constraints/specs.py`, not hardcoded lists
✅ Outcome: adding a new intent auto-appears in API response without endpoint code changes

### 16.3 Guardrail #3 — Metadata keys documented per RecipeSpec
- [x] Added `expected_metadata_keys` field to `RecipeSpec` dataclass
- [x] `DuplicatePORecipe.py` declares `("signal_scores", "matched_po_id")`
- [x] All RecipeSpecs have the field (even if empty)
- [x] Test fixtures in `conftest.py` include all declared keys
✅ Outcome: metadata drift is caught by CI

### 16.4 Guardrail #4 — ERP-agnostic gateway protocol
- [x] AST-based comment/docstring stripping for accurate code-only scanning
- [x] `gateways/base.py` code contains no ERP-specific terms (BAPI, RFC, SAP, Oracle, etc.)
- [x] `GatewayRequest`/`GatewayResponse` field names contain no ERP terms
- [x] `gateways/executor.py` code contains no ERP-specific terms
✅ Outcome: gateway protocol stays vendor-neutral for Oracle/Dynamics/WMS adapters

### 16.5 Guardrail #5 — Intent-agnostic exceptions table schema
- [x] SQLite migration introspection: no intent-specific column names
- [x] `resolution_data` extensibility column exists
- [x] PostgreSQL migration SQL: no intent-specific columns in exceptions DDL
✅ Outcome: adding a new intent requires zero schema changes

### 16.6 Guardrail #6 — Hierarchical policy key format
- [x] Regex validates `global.*`, `tenant.{id}.*`, `retailer.{id}.*`, `retailer.{id}.category.{cat}.*`
- [x] Flat keys (no scope prefix) rejected
- [x] Existing test_api.py policy keys follow the format
- [x] Database repository writes produce valid keys
✅ Outcome: V2 hierarchical resolution requires zero data migration

### 16.7 Invariant #11 — Recipes never import from policy
- [x] AST-based import check on all recipe files (not string matching in docstrings)
✅ Outcome: recipe-policy decoupling enforced at CI level

### 16.8 Supporting changes
- [x] `recipes/registry.py` — added `expected_metadata_keys` field to `RecipeSpec`
- [x] `tests/conftest.py` — added `matched_po_id` to DUPLICATE_PO fixture metadata

### 16.9 Tests
- [x] 25 new tests in `tests/test_v1_guardrails.py` (764 total)
✅ Outcome: all 6 guardrails + Invariant #11 enforced as CI gates

### 16.10 Documentation
- [x] `prompts/phase_16_v1_guardrails.md` — build prompt
- [x] `DESIGN.md` test coverage table — added test_v1_guardrails.py
- [x] `tasks.md` — this phase checklist
✅ Outcome: guardrail tests documented and reproducible
---
## REVIEW FINDINGS — Triple-Check Technical Review Board (2026-03-20)

### Critical
- [x] **TEST-1**: Add DUPLICATE_PO end-to-end graph tests (conftest fixture, test_graph_paths, test_golden, test_nodes for validate_types param mapping)

### High
- [x] **SEC-1**: Add structured logging to `compliance/shadow.py` audit() and enforce() methods so shadow decisions survive graph crashes

### Medium
- [x] **ARCH-1**: Externalize hardcoded business thresholds (15% discount, $5k exposure, 50-update/10k-variance circuit breaker) to `contracts/policy.py`
- [x] **ARCH-3**: Add fallback chain in `constraints/router.py` — degrade to DeterministicFallbackBackend on OutlinesConstrainedBackend init failure
- [x] **SEC-2**: Add explicit input validation at orchestration node boundaries (structured errors, not AttributeError)
- [x] **SEC-3**: Replace broad `except Exception: pass` in `graph.py` and `gateways/executor.py` with specific exception types and structured logging
- [x] **SEC-4**: Enforce `GatewayRequest.timeout_ms` in `GatewayExecutor.run()` via `concurrent.futures` thread timeout
- [x] **SEC-7**: Add SecretProviderClass and VolumeMount for Azure Key Vault CSI driver in `k8s/core/secret-provider.yaml` and `k8s/core/deployment.yaml`
- [x] **TEST-2**: Add UNKNOWN intent handling tests (should route to FAIL_TO_HUMAN)
- [x] **TEST-3**: Add recipe exception-throwing tests (verify RecipeExecutor catches and logs)

### Low (Board Verdict: SKIP — debated 2026-03-21)
- [~] **ARCH-2**: Replace `hasattr()` backend checks with Protocol/ABC — **SKIP**: 3+ files touched for zero behavioral change; import cycle risk; duck typing is idiomatic and tested
- [~] **ARCH-4**: Type `resolved_data` per gateway — **SKIP**: high blast radius (cross-cutting state model change); no code reads it unsafely; intentional loose typing for pass-through field
- [~] **ARCH-5**: Replace if/elif param mapping with registry — **SKIP**: premature abstraction at 3 recipes; CLAUDE.md says "three similar lines > premature abstraction"; 33 lines is readable
- [~] **SEC-5**: Add type/range validation to RecipeExecutor — **SKIP**: violates CLAUDE.md §1 (leaks recipe business logic into orchestration); exception handler already catches TypeError
- [~] **SEC-6**: Add signal score clamping to [0.0, 1.0] — **SKIP**: silent correction masks upstream bugs; classifications unaffected even with out-of-bounds scores; input is internally generated
- [~] **TEST-4**: Add adversarial input tests — **SKIP**: no known bugs caught; recipes already handle edge cases correctly; testing for testing's sake violates "smallest viable increment"
- [~] **TEST-5**: Validate compensation recipe names against registry — **SKIP**: two independent guards (Pydantic Literal + registry KeyError) already prevent the failure mode
---
## PHASE 18 — Server-Side User Profiles & Account Entity
Build prompt: prompts/phase_18_user_profiles.md
### 18.1 User Store (`api/users.py`)
- [x] `UserRecord` model: sub, email, name, title, avatar_initials, roles, org, assigned_accounts, env
- [x] 6 seed users: Jane Doe (admin), Marcus Webb (admin), Sarah Chen (manager), Sarah Chen Sr (analyst), James Ortiz (analyst, scoped to Walmart/Kroger), Priya Nair (analyst, scoped to Target/Costco)
- [x] Lookup helpers: `get_user_by_email()`, `get_user_by_sub()`, `list_users()`
- [x] `compute_visible_tabs()` — derives tab visibility from expanded permissions
- [x] `expand_permissions()` — mirrors `deps._expand_permissions()` for use outside request context
✅ Outcome: server-side user profiles replace hardcoded login credentials

### 18.2 Account Entity (`api/users.py`)
- [x] `Account` model: id, name, tenant_id, bp_number, tier, region
- [x] 4 seed accounts: Walmart (enterprise), Kroger (enterprise), Target (strategic), Costco (strategic)
- [x] Lookup helpers: `get_account()`, `get_account_by_name()`, `list_accounts()`
✅ Outcome: first-class customer entity within a tenant

### 18.3 Account Scoping
- [x] `GET /api/v1/accounts` endpoint (`api/routes/accounts.py`) — returns accounts filtered by user's `assigned_accounts`
- [x] `ExceptionRecord` (store.py) gains `account_id`, `account_name` fields
- [x] `ExceptionSummary`/`ExceptionDetail` (schemas.py) gain `account_id`, `account_name` fields
- [x] Exception list/detail endpoints filter by `assigned_accounts` when set
- [x] Partner role filters by `order_id` prefix in exception endpoints
✅ Outcome: users see only their assigned accounts and corresponding exceptions

### 18.4 Updated Auth Endpoints
- [x] `POST /api/auth/login` resolves against user store (not hardcoded)
- [x] `POST /api/auth/switch` — sandbox-only user switching (issues new JWT for target user, blocked in production)
- [x] `GET /api/auth/users` — sandbox-only user list (returns all seed user profiles, blocked in production)
✅ Outcome: auth flow uses real user records; sandbox supports user switching

### 18.5 JWT Claims & AuthenticatedUser
- [x] `AuthenticatedUser` (deps.py) gains `title`, `avatar_initials`, `assigned_accounts` fields
- [x] `create_access_token()` and `create_refresh_token()` include `title`, `avatar_initials`, `assigned_accounts` claims
- [x] `UserProfile` (schemas.py) gains `title`, `avatar_initials`, `assigned_accounts`, `visible_tabs` fields
- [x] New schemas: `AccountResponse`, `AccountListResponse`, `UserListResponse`
✅ Outcome: JWT carries user profile data; API responses include computed fields

### 18.6 Tests
- [x] 43 new tests in `tests/test_user_profiles.py` (1021 total)
- [x] User store CRUD, Account entity, JWT claims (title, avatar_initials, assigned_accounts)
- [x] Account scoping (assigned_accounts filtering), sandbox user switching, visible_tabs computation
✅ Outcome: full coverage of user profiles, accounts, scoping, and sandbox endpoints

### 18.7 Documentation
- [x] `DESIGN.md` §1 — `api/users.py` and `api/routes/accounts.py` in module structure
- [x] `DESIGN.md` §15.1 — new endpoints in route table (accounts, switch, users)
- [x] `DESIGN.md` §15.2 — user store, JWT claims, account scoping, sandbox endpoints
- [x] `DESIGN.md` §19 — `test_user_profiles.py` in test coverage table
- [x] `tasks.md` — this phase checklist
- [x] `README.md` — login credentials, new endpoints, sandbox user switching
✅ Outcome: docs cover user profiles, accounts, auth flow, and scoping

---

## PHASE 19 — Override Action Consolidation (Option A)

Three-phase body of work that fixed the Override feature, consolidated
Approve/Reject/Override into a single backend primitive, and added the
compliance controls the expert panels called for. Branch:
`claude/fix-override-action-agents-IkRPl`.

### 19.1 Option A gating — `/override` spans GREEN / YELLOW / RED
- [x] Extend `HITL_OVERRIDE_STATES` to include `RESOLVED` and `BLOCKED`
      so manager+ overrides are valid on GREEN-resolved and RED-blocked
      exceptions, not only YELLOW.
- [x] Explicit 409 `INVALID_VERDICT` guard — FAILED-lifecycle records
      have no agent decision to override.
- [x] Remove `OverrideRequest.resolved_by` (identity-spoofing vector);
      auditor identity always derives from `user.sub`.
- [x] Populate `recommended_action` in audit `previous_value` so the
      SOX "before" snapshot is meaningful (was always `None`).
- [x] `Idempotency-Key` header on `/override` — duplicate key + same
      body returns cached response; same key + different body → 409
      `IDEMPOTENCY_CONFLICT`. In-memory TTL cache; Redis is a Phase 3+
      hardening item.
- [x] New `POST /exceptions/{id}/escalate` endpoint with its own
      `EscalateRequest`, own audit event `EXCEPTION_ESCALATED`, own
      permission `exceptions:escalate` (analyst+). Decouples routing
      from resolution.
- [x] Eligibility-matrix tests (verdict × role × lifecycle × idempotency).
✅ Outcome: Override available on GREEN/YELLOW/RED for manager+;
   Escalate is a separate primitive; all trust-boundary bugs fixed.

### 19.2 Four-eyes for high-value overrides (Phase 2 #5)
- [x] `HIGH_VALUE_OVERRIDE_THRESHOLD_USD = 10_000.0` in
      `contracts/policy.py` (externalised; not hardcoded in a handler).
- [x] New lifecycle state `PENDING_COSIGN`.
- [x] `/override` branches: impact ≥ threshold → stash
      `resolution_data.pending_override` and transition to
      PENDING_COSIGN; below threshold → apply immediately.
- [x] New `POST /exceptions/{id}/override/cosign` endpoint with
      `CosignRequest { approve, notes }`. Gates: `exceptions:override`,
      caller ≠ `pending_override.initiator` (SoD), non-empty notes.
- [x] `EXCEPTION_OVERRIDE_INITIATED` / `EXCEPTION_OVERRIDE_COSIGNED` /
      `EXCEPTION_OVERRIDE_REJECTED` audit events.
- [x] Eight new four-eyes tests (low-value applies, high-value stages,
      cosign approve, cosign reject, SoD self-cosign rejected, analyst
      forbidden, non-pending 409, notes required).
✅ Outcome: SOX §404 management-override control in place; two reviewers
   required above the threshold.

### 19.3 Segregation of Duties
- [x] `/override` rejects a caller whose `user.sub` equals the record's
      prior `resolved_by` (excluding `system:*` principals so
      agent-auto-resolutions remain overridable). New 403
      `SOD_VIOLATION`.
- [x] Idempotency check moved above the SoD guard so retries of a
      successful first call still return the cached success.
✅ Outcome: a single manager cannot silently self-approve an alternate
   resolution of their own prior decision.

### 19.4 Controlled-vocabulary reason_tag
- [x] New `AllowedOverrideReasonTag` Literal in `constraints/specs.py`
      (`customer_concession`, `contract_stale`, `data_error`,
      `policy_exception`, `agent_misclassification`, `other`).
- [x] `DispositionRequest.reason_tag` validated against the vocabulary;
      422 `INVALID_REASON_TAG` on a bad value.
- [x] Persisted to the audit log `new_value` so downstream ML pipelines
      can cluster overrides by category without NLP on free-text notes.
- [x] `/health` exposes `allowed_override_reason_tags` at runtime for
      the UI chooser (Guardrail #2).
- [x] Phase 3 tightening: `reason_tag` is now required on the wire
      (was defaulting to `"other"` for Phase 2 compatibility).

### 19.5 OpenAPI-generated shared types (Phase 2 #9)
- [x] `scripts/export_openapi.py` dumps the FastAPI schema to
      `openapi/asoe2.openapi.json` with stable ordering.
- [x] Drift test `tests/test_openapi_contract.py` regenerates in-process
      and fails CI when the committed artifact is stale.
- [x] asoe-ui consumes via `openapi-typescript` → `src/types/generated.ts`
      with its own drift test.
✅ Outcome: every backend schema change forces a coordinated UI regen.
   Eliminates the class of bug that produced the original Override
   trust-boundary defect.

### 19.6 Unified `/disposition` primitive + retirement of legacy endpoints
- [x] New `PATCH /exceptions/{id}/disposition` with
      `DispositionRequest { action, notes, reason_tag }`.
      Server derives sub_type from (`chosen_action`,
      `recommended_action`):
      - `NO_ACTION`              → REJECT (exceptions:approve)
      - matches recommended      → APPROVE (exceptions:approve)
      - differs from recommended → OVERRIDE (exceptions:override,
        four-eyes applies)
- [x] Single `EXCEPTION_RESOLVED` audit event with `sub_type` on
      `new_value` — compliance tooling can answer "how often do managers
      deviate from the agent?" with one SQL query.
- [x] Phase 3 deletions (no backward compat): removed `/override`,
      `/approve`, `/reject` endpoints + `OverrideRequest`,
      `ApproveRequest`, `RejectRequest` schemas. Consolidated
      `HITL_APPROVE_STATES` + `HITL_REJECT_STATES` → single
      `HITL_DISPOSITION_STATES`. Dropped `EXECUTING` lifecycle state
      (only ever produced by the deleted `/approve`).
- [x] Test migration: 70+ call sites across 10 test files rewritten to
      use `/disposition`; audit assertions switched from
      `EXCEPTION_OVERRIDE` to `EXCEPTION_RESOLVED + sub_type`.
- [x] `/challenge` and `/admin-release` left intact — stakeholder-
      approved (Option A); different primitives (re-open / unblock,
      not resolve).
✅ Outcome: one disposition endpoint, distinct sub-type audit, legacy
   HITL surface retired. Lifecycle count 13 → 12.

### 19.7 Per-intent reason_tag framework (Phase 3 Option A)
- [x] `INTENT_REASON_TAGS: dict[str, tuple[str, ...]]` in
      `constraints/specs.py`, seeded with the global six-tag set for
      every `AllowedIntent`.
- [x] `/disposition` narrows validation to `INTENT_REASON_TAGS[intent]`
      when the record has a known intent; falls back to the global set
      otherwise (FAILED, unmapped).
- [x] `/health.allowed_override_reason_tags_by_intent` maps every
      intent → narrowed set. UI chooser filters by `detail.intent`.
- [x] Tests monkey-patch a narrower set to prove the mechanism; today's
      seeding produces no operator-visible change.
✅ Outcome: mechanism ready for a **data-only** curation follow-up (see
   Phase 5 — Deferred). Structural piece closed.

### 19.8 UX rename — "Override…" → "Decide…"
- [x] Voice-of-user research (v3 UX panel) found "override" carried
      negative connotation — analysts avoided the button. Visible label
      renamed to "Decide…"; aria-label and hover tooltip carry the
      long-form "Choose different action".
- [x] Approve button gains a hover tooltip previewing the recommended
      action ("Approve: Apply Contract Price") so the 1-click happy
      path is informed.
- [x] In-flight label "Overriding…" → "Deciding…".
- [x] Backend unchanged — `sub_type=OVERRIDE` continues to fire on
      chosen ≠ recommended.
✅ Outcome: queue-clearing speed preserved; naming friction removed.

### 19.9 Tests
- [x] 8 four-eyes tests in `tests/test_override_escalate.py`
- [x] 7 disposition sub-type tests
- [x] 2 SoD tests
- [x] 5 reason_tag vocabulary tests (incl. per-intent narrowing)
- [x] 1 OpenAPI drift test
- [x] 70+ migrated call sites across 10 existing test files
✅ Outcome: 1086 passed, 35 skipped. Audit event stream verified
   consistent with sub_type discriminator.

### 19.10 Documentation
- [x] `openapi/asoe2.openapi.json` regenerated (new `/disposition`,
      `/escalate`, `/override/cosign`; deleted `/override`, `/approve`,
      `/reject`).
- [x] `README.md` / `AUDITOR_GUIDE.md` / ADR — this Phase.
- [x] `tasks.md` — this checklist.

---

## PHASE 20 — Hash-Chained Append-Only Audit Log

### 20.1 In-memory hash chain (Phase 3 #3)
- [x] Every `policy_audit_log` entry carries
      `prev_hash` + `event_hash = sha256(prev_hash || canonical_json)`.
      First event per tenant chains from `GENESIS`.
- [x] `api/store.py::ExceptionStore.verify_audit_chain(tenant_id) →
      (valid, first_break_idx)` walks events and detects any mid-chain
      edit or deletion.
- [x] Per-tenant isolation — one tenant cannot contaminate another's
      chain.
- [x] 6 tests in `tests/test_audit_chain.py`.

### 20.2 SQL-layer hash chain + triggers (Phase 4)
- [x] V003 migration adds `prev_hash` + `event_hash` columns to
      `policy_audit_log` on both SQLite and PostgreSQL paths.
- [x] `BEFORE UPDATE` / `BEFORE DELETE` triggers raise
      `policy_audit_log is append-only` — rejects casual `psql`
      surgery without first dropping the trigger.
- [x] Backfill: walks existing rows in (tenant_id, created_at, id)
      order and assigns a real chain so the verifier reports valid from
      row 1 onward. Idempotent.
- [x] `db/repository.py::PolicyRepository._insert_audit_event` reads the
      tenant tail hash, writes `prev_hash` + `event_hash` on each INSERT.
- [x] `create_audit_event(...)` — audit-only path (no `policy_overrides`
      row); fixes a pre-existing bug where
      `DatabaseBackedStore.log_audit_event` spuriously wrote
      policy_override rows for application events.
- [x] `verify_audit_chain(tenant_id)` walks the SQL log in order,
      recomputes each row, returns `(True, None)` on a clean chain.
- [x] Shared `_audit_event_hash` helper — same canonical-JSON SHA-256
      algorithm as `api/store.py` and the V003 migration backfill. A
      parity test locks in the cross-implementation identity.
- [x] 8 tests in `tests/test_db_audit_chain.py` — columns present,
      GENESIS on first event, chain links, verify clean-log,
      per-tenant isolation, UPDATE trigger rejects, DELETE trigger
      rejects, cross-implementation hash parity.
✅ Outcome: tamper-evidence at both layers — application verification
   (`verify_audit_chain`) and database-level mutation rejection.
   Compliance reviewers can prove an audit row was neither edited nor
   deleted after write.

Phase 3 Option A shipped the **framework** for per-intent override-reason
vocabularies: `INTENT_REASON_TAGS` in `constraints/specs.py`, a
`/api/v1/health.allowed_override_reason_tags_by_intent` map, server-side
validation in `/disposition`, and a UI chooser that narrows by
`detail.intent`. Every intent today points at the full global set — so
the ML clustering signal is currently *no better than the global vocab*.

The next step is **data only** (no schema / API / UI work):

### 5.1 Stakeholder-curated categories
- [ ] Product / Compliance review a sample of historical override
      `change_reason` notes per intent.
- [ ] Propose 4–6 categories per intent that capture the common drivers.
      Example starting table (needs review, not committed):

      PRICE_MISMATCH       — customer_concession, contract_stale,
                             promo_window, data_error, other
      DUPLICATE_PO         — confirmed_separate_orders, customer_intent,
                             data_error, other
      CREDIT_BLOCK         — prepayment_received, credit_limit_review,
                             customer_relationship, other
      BACK_ORDER           — partial_fulfillment_ok, substitute_sku_offered,
                             customer_waived, other

- [ ] `other` must remain in every intent's set (prevents workflow
      dead-ends when the real reason doesn't fit a listed category).

### 5.2 Wiring
- [ ] Replace the `INTENT_REASON_TAGS = {i: _GLOBAL_REASON_TAGS for i in ...}`
      seeding in `constraints/specs.py` with the curated table.
- [ ] Regenerate `openapi/asoe2.openapi.json`.
- [ ] `cd ../asoe-ui && npm run generate-types` — no manual TS changes
      required (the `allowed_override_reason_tags_by_intent` field is
      already in the frontend's HealthResponse type).
- [ ] Update the narrowing test `TestPerIntentReasonTag` to exercise the
      new vocabulary.

### 5.3 Audit of existing rows
- [ ] Existing audit events carry reason_tags that were valid under the
      global set. After curation, some of those tags may no longer be
      valid for their intent. Decide: grandfather them (recommended —
      the chain's tamper-evidence must not be retroactively invalidated)
      or re-label. The grandfather path is purely a validator change
      on new writes; existing rows are immutable by DB trigger anyway.

### 5.4 ML signal activation
- [ ] Once curated categories land, the ML team can cluster
      `(intent, reason_tag)` tuples for meaningful override-pattern
      analysis. This is the outcome the v3 expert panel's
      Data / ML-signal voice asked for — Option A delivered the
      mechanism, Phase 5 delivers the value.

🕰️  Owner: product/compliance jointly; ML engineering consumes the
    resulting signal.

---

## PHASE 21 — OM Coverage Expansion: PRICE_HOLD_RELEASE + EDI_MISMATCH

Moves Order Management coverage from *Partial* (the 4 canonical
intents described real OM exception classes only by approximation) to
*Complete* by adding two first-class intents that no existing intent
could express without overloading semantics.

- Price Hold Release: distinct from CONTRACTUAL_CORRECTION — the
  latter *changes* a price; this one decides whether to *release* an
  order currently held because the PO price is outside tolerance of
  the SAP base.
- EDI Mismatch: distinct from DUPLICATE_PO — DUPLICATE is one
  sub-type of EDI 850 variance; SKU / QTY / UOM / SHIP_TO mismatches
  are their own classes with their own resolution paths. See
  `docs/adr/ADR-024-om-coverage-expansion.md` for the decision
  rationale and the single-source-of-truth invariant that shaped the
  PRICE_MISMATCH routing fork.

### 21.1 Contracts + policy
- [x] Extend `Intent` enum in `contracts/models.py` with
      `PRICE_HOLD_RELEASE` and `EDI_MISMATCH`.
- [x] Add policy thresholds to `contracts/policy.py`:
      `PRICE_HOLD_TOLERANCE_PCT` (0.02), `PRICE_HOLD_HARD_BLOCK_PCT`
      (0.10), `EDI_MISMATCH_AUTONOMY_LEVELS` (L1/L2/L3 per sub_type).

### 21.2 Recipes
- [x] `recipes/PriceHoldReleaseRecipe.py` — three-branch decision
      (AUTO_RELEASE / ESCALATE / HARD_BLOCK) keyed on
      `|variance_pct|` vs tolerance / hard_block thresholds. Pure
      function, no I/O, no imports from `contracts.policy`.
- [x] `recipes/EdiMismatchRecipe.py` — sub_type → classification
      mapping (SKU=HARD_REJECT, QTY/UOM=REVIEW, SHIP_TO=ESCALATE).
      `PRICE_MISMATCH` deliberately absent from the accepted
      vocabulary — routed at classifier time (see §21.4).

### 21.3 Constrained generation
- [x] `constraints/specs.py` — extend `AllowedIntent` and
      `AllowedRecipeName`; add `AllowedEdiMismatchSubType`,
      `AllowedEdiMismatchClassification`, `AllowedPriceHoldAction`.
      CLAUDE.md §3 requires every machine-consumed output field to be
      Literal-gated.

### 21.4 Classifier + skill routing fork (PRICE_MISMATCH → CONTRACTUAL_CORRECTION)
- [x] `constraints/fallback_backend.py:classify_intent` inspects
      `event.metadata.mismatch_sub_type`; when it equals
      `"PRICE_MISMATCH"` the event is classified as
      `CONTRACTUAL_CORRECTION` so `PriceAdjustmentRecipe.py` handles
      pricing — preserving the single source of truth for price
      corrections (CLAUDE.md §1). `PRICE_MISMATCH` is therefore
      statically unreachable inside `EdiMismatchRecipe`.
- [x] `skills/loader.py:select_for_event` mirrors the fork at the
      skill layer so skill text matches the assigned intent.

### 21.5 Skills + registry + orchestration
- [x] `skills/price-hold-release_SKILL.md`, `skills/edi-mismatch_SKILL.md`.
- [x] `recipes/registry.py` — two new `RecipeSpec` entries with
      gateway dependencies (`oms/get_price_hold_status`) and effects
      (`oms/update_hold_flag`, `buyer_notification/send`).
- [x] `orchestration/nodes.py:validate_types` — two new `elif` arms
      plus an explicit final `else` that FAIL_TO_HUMAN-s on an
      unwired-but-known recipe name (closes the silent-trap the old
      fall-through behaviour represented).

### 21.6 Observability
- [x] Shadow policy-hit vocabulary added to
      `constraints/fallback_backend.py`: `PRICE_HOLD_TOLERANCE_OK`,
      `PRICE_HOLD_TOLERANCE_ESCALATE`, `PRICE_HOLD_HARD_BLOCK`,
      `EDI_SKU_MISMATCH_HARD_REJECT`, `EDI_QTY_MISMATCH_REVIEW`,
      `EDI_UOM_MISMATCH_REVIEW`, `EDI_SHIP_TO_ESCALATE`. Visible on
      `TraceRecord.shadow_policy_hits` and the
      `/api/v1/exceptions/{id}/trace` response.

### 21.7 Tests
- [x] Unit: every branch of both recipes.
- [x] Validate-types: every new `elif` arm + the explicit `else` trap.
- [x] E2E: `tests/test_e2e_price_hold_release.py` (3 action branches +
      disposition + trace shape), `tests/test_e2e_edi_mismatch.py`
      (4 sub_type branches + PRICE_MISMATCH routing assertion + trace
      shape + stats aggregation).
- [x] Registry / skill-loader / intent-classifier fitness tests.

### 21.8 Sandbox
- [x] `tests/sandbox/seed.py` — 9 new events (3 PHR branches +
      4 EDM sub_types + PRICE_MISMATCH routing demo + invalid
      sub_type).
- [x] CLI: `--intent PRICE_HOLD_RELEASE`, `--intent EDI_MISMATCH`
      filter maps + `_intent_label` prefix branches.
- [x] Sandbox UI: skill-text map, `_intent_label`, custom-event form
      selector (incl. `mismatch_sub_type` dropdown that demonstrates
      the routing fork).

### 21.9 UI integration (asoe-ui, tracked separately)
- [x] `src/types/exceptions.ts` — `PriceHoldAnalysisData`,
      `EdiMismatchAnalysisData` + optional fields on `OrderAnalysis`.
- [x] `src/app/exceptions/PriceHoldSection.tsx`,
      `EdiMismatchSection.tsx` — data-presence enrichment, wired in
      `ExceptionDetailPanel`.
- [x] `src/config/erp-label-map.ts`, `src/hooks/useErpProfile.ts` —
      vendor-specific display labels (SAP / Oracle / Salesforce /
      GENERIC) driven by `NEXT_PUBLIC_ASOE_ERP_VENDOR`. Default set
      to `SAP` in `next.config.mjs`. Canonical backend codes unchanged.

✅ Outcome: OM coverage Complete. PRICE_MISMATCH routing fork protects
the pricing single-source-of-truth at classifier, skill, and UI
rendering layers — a UI regression is blocked by the e2e contract
test that asserts a PRICE_MISMATCH fixture lands under
`CONTRACTUAL_CORRECTION`, not `EDI_MISMATCH`.

---

## PHASE 22 — UI-Backend Intent Parity: BACK_ORDER, OVER_MAX, MIN_ORDER_QTY, PALLET_CONFIG, DELIVERY_DELAY

Closes the cross-repo contract drift flagged by the architecture
review (C1): the asoe-ui repo had shipped 5 intents in Phase 8.10 that
had no backend support. This phase brings the backend to parity —
every intent asoe-ui classifies is now backend-classified, recipe-
executed, shadow-audited, and covered by e2e tests.

### 22.1 Contracts + policy
- [x] `contracts/models.py::Intent` extended with `BACK_ORDER`,
      `OVER_MAX`, `MIN_ORDER_QTY`, `PALLET_CONFIG`, `DELIVERY_DELAY`.
- [x] `contracts/policy.py` — 8 new thresholds mapping to prototype
      spec rule IDs (SD-OOS-001/002, SD-OM-001/002, SD-MOQ-001/002,
      SD-PLT-001/002, SD-DELAY-001/002).

### 22.2 Recipes
- [x] `recipes/BackOrderResolutionRecipe.py` — classifies gap
      (NO_GAP/MINOR_GAP/SEVERE_GAP); ranks resolution options
      (ALT_DC, SUBSTITUTE, SPLIT_SHIPMENT, RESCHEDULE) on a weighted
      composite score of service / revenue / logistics.
- [x] `recipes/OverMaxTrimRecipe.py` — two-phase trim plan: per-line
      ceiling trim, then proportional distribution across even-layer
      lines; broken-layer lines get `action=SKIP` to preserve pallet
      integrity.
- [x] `recipes/MOQRoundUpRecipe.py` — three-way
      ROUND_UP / ACCEPT_BELOW / ESCALATE; uplift-review gate catches
      large-value round-ups even when shortfall is MINOR.
- [x] `recipes/PalletAlignmentRecipe.py` — per-line fill-% detection
      for BROKEN_LAYER vs PARTIAL_PALLET vs MIXED; recommends round-
      down plan.
- [x] `recipes/DeliveryDelayResolutionRecipe.py` — days-late
      classification (ON_TIME / MINOR / SEVERE); option ranker
      prefers EXPEDITE for MINOR, RESCHEDULE for SEVERE unless the
      caller pins a recommended option.

### 22.3 Constrained generation + registry
- [x] `constraints/specs.py` — `AllowedIntent` + `AllowedRecipeName`
      Literals extended.
- [x] `recipes/registry.py` — 5 new `RecipeSpec` entries with
      declared gateway dependencies where applicable.
- [x] Prompt surfaces updated for Guidance, Outlines, and local LLM
      backends.

### 22.4 Classifier + skill routing
- [x] `constraints/fallback_backend.py::classify_intent` — 5 new
      event-type → intent branches (BACK_ORDER_OOS, OVER_MAX_QTY,
      MIN_ORDER_QTY, PALLET_CONFIG_VIOLATION, DELIVERY_DELAY).
- [x] 5 new `shadow_decision` helpers computing verdicts from
      event metadata (gap_pct / exceedance / shortfall / fill% /
      days_late). New `shadow_policy_hits` tag vocabulary
      (BACK_ORDER_SEVERE_GAP, OVER_MAX_SEVERE_EXCEEDANCE, etc.).
- [x] `skills/loader.py` — 5 new event-type → skill branches with
      ordering preserved (DUPLICATE → PRICE_HOLD → LINE_MISMATCH →
      OM-adjacent → generic).

### 22.5 Skills + orchestration
- [x] 5 new `skills/*.md` files following the canonical six-section
      structure.
- [x] `orchestration/nodes.py::validate_types` — 5 new `elif` arms
      injecting policy thresholds into `RecipeInvocation.params`.
      Explicit final `else` preserved.

### 22.6 Golden-test dynamization
- [x] `tests/test_constraints.py` intent_regex / recipe_name_regex
      goldens refactored to derive from `AllowedIntent.__args__` /
      `AllowedRecipeName.__args__` — no more brittle string goldens
      that require a parallel sweep when vocabulary grows.
- [x] `tests/test_registry.py::test_registry_size_matches_allowed_recipe_name_literal`
      compares registry size against `len(AllowedRecipeName.__args__)`.
- [x] `tests/test_executor.py::test_registered_names_returns_all`
      iterates `AllowedRecipeName.__args__`.
- [x] `tests/test_v1_guardrails.py::_INTENT_LITERALS` derived
      dynamically from `AllowedIntent.__args__` — addresses
      cross-repo review C5 (static-analysis guardrail had holes for
      new intents).

### 22.7 Tests
- [x] Unit: every branch of each new recipe
      (`tests/test_recipes.py`).
- [x] Skill-loader: `TestOMAdjacentIntentRouting` for all 5 new event
      types (`tests/test_skill_loader.py`).
- [x] E2E: `tests/test_e2e_om_adjacent_intents.py` — 17 cases
      covering health, resolve minor/severe branches, trace policy
      hits, stats aggregation.

### 22.8 Sandbox + docs
- [x] `tests/sandbox/seed.py` — 8 new events (EVT-BO-001/002,
      EVT-OM-001/002, EVT-MOQ-001/002, EVT-PLT-001, EVT-DD-001/002)
      covering MINOR + SEVERE branches across the intents.
- [x] `tests/sandbox/cli.py` — `_intent_label` prefix branches and
      `intent_prefix_map` for `--intent` filter.
- [x] `tests/sandbox/ui/app.py` — skill-text map, intent-label
      branches, custom-event form event_type options with default
      metadata per intent.
- [x] `README.md` / `docs/AUDITOR_GUIDE.md` / `DESIGN.md` updated
      with the 5 new intents + recipes + policy thresholds.

✅ Outcome: asoe-ui's 11-intent mock-health vocabulary now matches
the asoe2 backend reality. The `NEXT_PUBLIC_SHOW_PREVIEW_INTENTS`
gate proposal (asoe-ui backlog) is no longer needed — kept as a
design note in case a future wave of speculative UI outpaces backend
again.

## PHASE 23 — Verdict three-pillar architecture: enrichment composer + registry enforcement

Closes the cross-repo Verdict (2026-04-22 compliance workshop).
Recipes stay execution-only; a new graph node assembles the
analysis payload; a registry classifies every UI field by
audit-bearing / conditional / contextual; the UI renders honestly
without dashes.

### 23.1 Compliance registry + enforcement
- [x] `compliance/audit_bearing_registry.yaml` — 107 fields
      classified across 8 `*AnalysisData` classes, with
      conditional predicates and grandfather clauses.
- [x] `.github/CODEOWNERS` routes `compliance/**` to
      `@compliance-team` (Dana's mandate #1).
- [x] `tests/test_audit_registry_coverage.py` (8 fitness tests)
      blocks silent schema additions.

### 23.2 Pillar 1 — enrichment_context persistence
- [x] `GraphState.enrichment_context: Dict[str, Any]` field.
- [x] `ExceptionRecord.enrichment_context` attribute persisted via
      `api/store.py` (in-memory + DB store).
- [x] `_persist_exception` bridge: prefer explicit context, fall
      back to legacy `state.resolved_data` for non-breaking
      rollout.

### 23.3 Pillar 2 — CQRS read model + AUDIT_CONTEXT_MISSING terminal
- [x] `TerminalStatus.AUDIT_CONTEXT_MISSING` distinct from
      FAIL_TO_HUMAN (Dana's mandate #2).
- [x] `api/analysis_composer.py` — pure registry-aware projection
      with grandfather-clause date check.
- [x] `orchestration/nodes.py::build_analysis` — terminal node on
      every graph path; routes to AUDIT_CONTEXT_MISSING on coverage
      failure.
- [x] `orchestration/graph.py` — every `terminal: END` edge
      replaced with `terminal: build_analysis → END`.
- [x] `/analysis` endpoint consumes the composer; suppresses
      partial projections so the UI never receives half-truth.
- [x] `TraceResponse.audit_context_missing_{class,fields}` —
      structured trace surface so auditors don't regex prose.

### 23.4 Adapter chain (six of ten enrichment sections)
- [x] `adapt_price_hold` (PriceHoldReleaseRecipe → price_hold_analysis).
- [x] `adapt_edi_mismatch` (EdiMismatchRecipe → edi_mismatch_analysis).
- [x] `adapt_delivery_delay` (DeliveryDelayResolutionRecipe →
      delivery_delay_analysis). Grandfather: at_risk, sla_deadline.
- [x] `adapt_overmax` (OverMaxTrimRecipe → overmax_analysis).
      Grandfather: contract_ref, block_status, block_reason,
      order_lines, trim_plan, uom.
- [x] `adapt_moq` (MOQRoundUpRecipe → moq_analysis). Grandfather:
      moq_source, channel, contract_ref, block_status.
- [x] `adapt_pallet` (PalletAlignmentRecipe → pallet_analysis).
      No grandfather — recipe + UI shapes are 1:1.

### 23.5 Remaining adapters (gateway-dependent — out of this phase)
- [ ] `adapt_price` (PriceAdjustmentRecipe → price_analysis).
      Blocked on price_analysis_gateway_gap clause + 2026-06-21
      deadline.
- [ ] `adapt_duplicate` (DuplicatePORecipe → duplicate_detection).
      Blocked on persisting matched_po_details on the record.
- [ ] `adapt_order_comparison` — synthesised from duplicate; lands
      after adapt_duplicate.
- [ ] `adapt_back_order` (BackOrderResolutionRecipe →
      backorder_analysis). Blocked on persisting warehouse snapshots.

### 23.6 CLAUDE.md guardrails
- [x] Guardrail 6: "UI richness is a strict product commitment";
      do not prune `*AnalysisData` classes; `build_analysis` is
      sole assembler. Cited in code review as reason code 'G6'.

✅ Outcome: 6 of 10 enrichment sections backend-backed end-to-end
with Pillar 1 + Pillar 2 + Pillar 3 enforced. The remaining 4
sections need gateway-persistence work in a future phase.



## PHASE 24 — Verdict Full-Close (retire all grandfather clauses + ADR-025 graph reorder)

**Companion to:** asoe-ui Phase 8.12 (mock pipeline + audit-gap surface sync).

Phase 23 closed 6 of 10 enrichment sections; four remained mock-only
and four grandfather clauses were still active. Phase 24 closes the
Verdict commitment end-to-end.

### 24.1 Foundation — single-bag enrichment_context (T1)
- [x] `orchestration/nodes.py::resolve_dependencies` writes only
      to `state.enrichment_context`. `state.resolved_data` no
      longer carries gateway results.
- [x] DuplicatePO + PriceHoldRelease recipe-input nodes read from
      `enrichment_context` (one bag).
- [x] `db/migrations/V004__enrichment_context.sql` — adds
      `enrichment_context JSONB NOT NULL DEFAULT '{}'` on
      `exceptions`. SQLite parity in `db/migrations/runner.py`.
- [x] `db/repository.py::ExceptionRepository.create()` threads
      `enrichment_context` through.
- [x] `api/store.py::DbExceptionStore.create` drops the in-memory
      bridge.
- [x] `api/routes/exceptions.py::_persist_exception` drops the
      `resolved_data → enrichment_context` fallback.
- [x] Tests: `test_enrichment_context.py` extended for single-bag
      semantics.

### 24.2 DuplicatePO + OrderComparison adapters (T2)
- [x] `api/schemas.py` — `OrderSnapshot`, `DuplicateDetectionData`,
      `ComparisonOrder`, `ComparisonLineItem`,
      `OrderComparisonData` Pydantic models. New
      `AnalysisResponse` fields.
- [x] `api/analysis_adapters.py` — `adapt_duplicate` (primary,
      audit enforcement target) + `adapt_order_comparison`
      (synthesised from same `matched_po_details` payload).
- [x] `SECONDARY_ANALYSIS_ADAPTERS` registry — pattern for
      derived projections sharing the primary's attestation
      target.
- [x] Synthesis fallback in `adapt_duplicate` (pure recipe call)
      for explain-mode + shadow-gated paths.
- [x] Explain graph wires `resolve_dependencies` so dry-run audit
      enforcement sees the same evidence the live path would.
- [x] Tests: `test_analysis_adapters_duplicate.py` (14 cases).

### 24.3 BackOrder adapter (T3)
- [x] `api/schemas.py` — `WarehouseInfo`, `AlternateWarehouse`,
      `SubstituteSKU`, `InboundOrder`, `ResolutionOption`,
      `BackOrderAnalysisData`.
- [x] `api/analysis_adapters.py` — `adapt_back_order` with
      synthesis fallback.
- [x] `INTENT_TO_RECIPE_NAME["BACK_ORDER"]`.
- [x] Tests: `test_analysis_adapters_back_order.py` (10 cases).

### 24.4 Price adapter + retire price_analysis_gateway_gap (T4)
- [x] `recipes/registry.py` — `PriceAdjustmentRecipe` gains 3
      `GatewayDependency` entries (`sap_doc`, `sap_contract`,
      `promotion`).
- [x] `api/schemas.py` — `PriceAnalysisData` populated from
      `sap_doc_context` / `contract_context` / `promotion_context`.
- [x] `compliance/audit_bearing_registry.yaml` — retire
      `price_analysis_gateway_gap`; reclassify `contract_ref` +
      `promotion_ref` audit-bearing → contextual (conditionally
      present per registry rationale).
- [x] `orchestration/nodes.py::build_analysis` preserves
      FAIL_TO_HUMAN against AUDIT_CONTEXT_MISSING override —
      circuit-breaker / validation failures stay debuggable.
- [x] Tests: `test_analysis_adapters_price.py` (14 cases) +
      `test_audit_registry_coverage.py` summary tally re-verified.

### 24.5 Retire delivery_delay / overmax / moq clauses (T5)
- [x] `recipes/registry.py` — `DeliveryDelayResolutionRecipe`,
      `OverMaxTrimRecipe`, `MOQRoundUpRecipe` gain
      `GatewayDependency` entries (`sla_contract`,
      `sap_contract` + `sap_block`, `sap_customer_master` +
      `sap_contract` + `sap_block`).
- [x] `api/analysis_adapters.py` — `adapt_delivery_delay` /
      `adapt_overmax` / `adapt_moq` extended to project the
      previously-grandfathered fields from the new gateway
      result keys; metadata fallback retained for shadow-gated
      paths.
- [x] `compliance/audit_bearing_registry.yaml` — retire
      `delivery_delay_financial_gap`, `overmax_gateway_gap`,
      `moq_gateway_gap`. The `grandfather_clauses` block is now
      empty.
- [x] Tests: `test_analysis_adapters_t5.py` (8 cases).

### 24.6 ADR-025 — Gateway READS before shadow_audit
- [x] `orchestration/graph.py` — common section reorders to
      `select_recipe → resolve_dependencies → validate_types →
      shadow_audit`, then variant-specific post-shadow
      continuation (`execute_recipe + apply_effects` in live;
      `explain_only` in explain).
- [x] `contracts/models.py` — `GraphState.request_trace_id` (UUID
      stamped at `ingest`) for gateway-call correlation
      independent of `shadow.trace_id` (which doesn't exist when
      `resolve_dependencies` runs in the new order).
- [x] `contracts/models.py` — `GatewayDependency.required_for_audit`
      (default True). Soft-fail path in `resolve_dependencies`
      writes empty dict + lets composer route to
      AUDIT_CONTEXT_MISSING via the standard coverage check.
- [x] `select_recipe` no longer terminates on no-recipe — shadow
      gets to be the terminal voice for compliance-only intents
      (MASS_PRICING_ERROR / UNKNOWN). `execute_recipe`'s
      invocation-None guard preserves the upstream explanation.
- [x] `docs/adr/ADR-025-gateway-reads-before-shadow.md` — full
      rationale + Guardrail #4 reinterpretation + consequences.

### 24.7 Sandbox infrastructure
- [x] `api/sandbox_gateways.py` (new) — `register_sandbox_gateways()`
      mirrors `tests/conftest.py` StubGateways (oms,
      buyer_notification, sap_doc, sap_contract, promotion,
      sap_block, sap_customer_master, sla_contract). Called from
      `create_app()` inside the `ASOE_ENV=sandbox` block.
      Idempotent.
- [x] `db/migrations/runner.py` — drop the V1 `intent` CHECK
      constraint from the SQLite schema. Intent enum at
      `contracts/models.py` is the source of truth; CHECK drifted
      every time a new intent shipped.
- [x] `tests/test_db.py` — `test_intent_check_constraint` rewrite
      → `test_intent_persists_verbatim` covering the full
      post-V1 intent set.

### 24.8 Test suite
- [x] Per-tranche adapter tests landed (T2 + T3 + T4 + T5):
      14 + 10 + 14 + 8 = 46 new cases.
- [x] `test_e2e_om_adjacent_intents.py` shadow-gated paths revert
      to expecting BLOCKED / MANUAL_REVIEW_REQUIRED (post-ADR-025
      audit evidence is captured even on shadow-gated paths, so
      the composer no longer over-routes to AUDIT_CONTEXT_MISSING).
- [x] `test_analysis_composer.py` grandfather-clause tests
      rewritten for post-retirement reality.

✅ Outcome: `grandfather_clauses` block in
`compliance/audit_bearing_registry.yaml` is empty. 10 of 10
enrichment sections backend-backed. Suite 1343 passed, 35 skipped
(+52 net new vs pre-engagement). Local sandbox e2e walkthrough
works end-to-end via stub gateways. (2026-04-25)



## PHASE 25 — Remote LLM Provider Tier (V1 PR-1)

Build prompt: `prompts/pre_code_session.md`. Branch:
`claude/add-llm-support-h2t9i`.

Generalises the constraint backend to a per-task,
provider-agnostic router so every trio call (`classify_intent` /
`propose_recipe` / `shadow_decision`) can be served by Anthropic,
OpenAI / Azure OpenAI / vLLM, Ollama, HuggingFace, or the
deterministic fallback — runtime-switchable via env vars without
redeploying. Default is `fallback` (no behavior change unless an
operator opts in). Five expert reviews (architect, security &
compliance, cost / ops, Claude API skill, triple-check board) + the
user's own design decisions shaped the scope.

### 25.1 Provider abstraction (S3a + S3d + S3e)
- [x] `llm/provider_protocol.py` — `LLMProviderClient` Protocol +
      `ToolCallResult` / `SystemBlock` / `CacheControl` /
      `TokenUsage` / `ProviderError` dataclasses. Constraints layer
      sees only this — no vendor SDK leaks upward.
- [x] `llm/anthropic_client.py` — direct + Foundry, `claude-sonnet-4-6`
      default. Tool-use forced via `tool_choice`. Cache_control
      ephemeral on cacheable system blocks.
- [x] `llm/openai_client.py` — full OpenAI / Azure OpenAI /
      OpenAI-compatible (vLLM, TGI, LiteLLM, LocalAI). Auto-detects
      Azure when `OPENAI_API_VERSION` is set. Surfaces
      `prompt_tokens_details.cached_tokens` (OpenAI auto-caching).
- [x] `llm/ollama_client.py` — full self-hosted + Cloud. OpenAI-style
      tool calling on Qwen2.5+, Llama 3.1+, Mistral.
- [x] `llm/huggingface_client.py` — full HF Dedicated Inference
      Endpoints + Serverless Inference API (production blocks the
      latter).
- [x] `llm/google_client.py` — V1 stub (Vertex AI / Gemini wiring
      in a follow-up).
- [x] `llm/provider_factory.py` — `PROVIDER_FACTORIES` registry +
      `build_provider_client(provider)`.
- [x] Per-provider env-var prefix pattern (`ANTHROPIC_*` /
      `OPENAI_*` / `OLLAMA_*` / `HUGGINGFACE_*` / `GOOGLE_*`) with
      `RemoteLLMConfig.from_env(provider="...")`.
- [x] Production-egress allowlists: api.anthropic.com /
      api.openai.com / public Ollama Cloud / HF Serverless
      Inference / public Gemini all blocked when `ASOE_ENV=production`.
- [x] Lazy SDK imports — every client module is importable without
      its provider's package; SDK only loads inside `from_config()`.

### 25.2 LLM utilities (S2)
- [x] `llm/sanitizer.py` — OrderEvent.metadata allowlist +
      length-cap (256 chars) + control-char scrub +
      untrusted-data delimiter (Chen review §5 prompt-injection
      mitigation).
- [x] `llm/budget.py` — InMemoryBudgetTracker +
      RedisBudgetTracker; daily USD spend cap with soft-warn (80%)
      / hard-block (100%) thresholds; Redis errors degrade safely.
- [x] `llm/circuit_breaker.py` — LLM-tier breaker, sliding 60s
      window, error-rate > 25% / p95 > 15s trip, 5-min cooldown,
      HALF_OPEN probe with `_probe_in_flight` flag.

### 25.3 Constraint-layer integration (S3b + S3c + S4)
- [x] `constraints/llm_backend.py` — `RemoteLLMBackend`
      composes any `LLMProviderClient` + sanitiser + breaker +
      budget; trio surface; falls through to deterministic on
      every failure mode.
- [x] `constraints/router.py` — `get_constrained_backend(task)`
      with full per-task routing (kill-switch + explain-mode at
      the top, then `ASOE_LLM_DISABLE_FOR`, then per-task /
      global env, then USE_OUTLINES_BACKEND legacy, then
      fallback).
- [x] `constraints/cross_check.py` — pure-function comparator;
      orchestration `classify` runs deterministic in parallel
      and routes to MANUAL_REVIEW_REQUIRED on disagreement.
- [x] `tests/test_llm_cache_invalidators.py` — byte-identical
      system+tools prefix audit (panel-blocked CI guard against
      cache-hit-rate regressions).

### 25.4 Orchestration wiring (S4)
- [x] `orchestration/nodes.py` — per-task `_backend(task)` cache;
      `classify` → `intent`, `select_recipe` → `recipe`,
      `shadow_audit` → `shadow`. Cross-check inline. Drains
      `last_call_trace` onto state after each call.
- [x] `compliance/shadow.py` — default backend uses
      `get_constrained_backend(task='shadow')`.
- [x] Kill-switch + explain-mode pinning verified end-to-end
      (no provider client constructed when either gate is active).

### 25.5 SOX-grade telemetry (S5a + S5b)
- [x] `contracts/models.py` — `LLMCallTrace` Pydantic model with
      provider / model_id / request_id / token usage / cache hits /
      cost / fallback flags / cross-check signals.
      `GraphState.llm_call_traces: List[LLMCallTrace]`.
- [x] `RemoteLLMBackend.last_call_trace` populated at every
      `_invoke` exit branch (success, ProviderError,
      CircuitOpen, budget hard-block, validation error). SHA-256
      `prompt_hash` + `tool_call_hash` + `skill_md_version` for
      cross-pod reproducibility audits — never logs prompt content.
- [x] `observability/tracer.py` — `TraceRecord.llm_calls` +
      aggregate scalars (token totals, cost, fallback flag,
      disagreement flag).
- [x] `observability/langfuse_sink.py` — emits one
      `generation`-typed observation per `LLMCallTrace` on both
      v2 and v4 LangFuse SDK paths. Native LangFuse fields
      (`model`, `usage`); audit / fallback / cross-check signals
      in `metadata`. Prompt content NEVER forwarded — only hashes.

### 25.6 Compliance audit registry (S5c)
- [x] `compliance/audit_bearing_registry.yaml` — new
      `LLMProvenance` section with 3 audit-bearing rows
      (`llm_provider_used`, `llm_model_id`, `llm_request_id`),
      `pending_signoff: true` until the workshop follow-up
      flips to false. Summary tally updated 107 → 110, 82 → 85.

### 25.7 Docs (S5d)
- [x] `DESIGN.md` §1 module map, §2 backend chain, §9
      observability, §12 env-var reference, §19 test coverage.
- [x] `architecture_v3.md` §5.3 per-task router + provider
      matrix + cross-check + cost guardrails; §18 env vars.
- [x] `.env.example` — full provider env-var inventory.
- [x] `pyproject.toml` — `[anthropic, openai, ollama, huggingface]`
      optional dependency groups.

### 25.8 Test deltas
+249 net new tests across S2 / S3 / S4 / S5. Final suite: 1592
passed, 35 skipped (vs Phase 24 baseline 1343 passed, 35 skipped).
Zero regressions. All provider tests are network-free
(sys.modules SDK stubs); golden-path graph tests still pass with
default `ASOE_LLM_PROVIDER=fallback`.

✅ Outcome: ASOE has a per-task, provider-agnostic remote-LLM
tier that operators can flip on per environment / per tenant /
per task / per call without redeploying. Sandbox shakeout default
is `fallback` (no spend, no egress); the panel-required
hardening (kill-switch + explain-mode pinning + production
egress block + audit registry + cross-check + budget cap +
circuit breaker) is in place. Production rollout requires only
the LLMProvenance compliance sign-off + the operator's per-tenant
provider config.

---

## PHASE 26 — Post-deploy fixes + operational hardening (in flight)

This phase tracks fixes and hardening that landed after
architecture_v3.md was finalised. Items are shipped on
`core_ui_integration` unless otherwise noted; v4 absorbs them.

### 26.1 Env-driven JWT TTLs (operator-tunable)
- [x] `_resolve_token_ttls()` pure function in `api/deps.py` —
      env-driven access/refresh TTL resolution.
      Defaults: sandbox 24h access / 30d refresh; production
      60min access / 7d refresh. Read from
      `ASOE_ACCESS_TOKEN_TTL_SECONDS` and
      `ASOE_REFRESH_TOKEN_TTL_SECONDS`.
- [x] Defensive against empty / malformed / zero / negative
      values — falls back to per-environment defaults rather than
      crashing on bad operator input.
- [x] `infra/main.bicep` — `accessTokenTtlSeconds` /
      `refreshTokenTtlSeconds` params surfaced as `@secure()`
      strings, wired as env vars on the API container. Operator-
      friendly presets (15min / 1h / 24h) documented in the
      bicep deployment guide.
- [x] Tests cover the empty-string, malformed-int, zero, and
      negative branches plus the env-driven happy path.
✅ Outcome: JWT TTL is configurable without redeploy for sandbox
shakeout vs production posture. (See ADR-021 §JWT for the
deployment contract.)

### 26.2 Confidence persistence + scaled read (Verdict 2026-04-22 / Guardrail #6)
- [x] `api/routes/exceptions.py` resolve write site (~L229) —
      persist `intent_confidence: state.confidence` (0.0-1.0
      float) into `trace_data` alongside `intent_selected`.
- [x] `api/routes/exceptions.py` reanalyze write site (~L1245)
      — same persistence on the reanalysis path so reanalysed
      records carry the new attempt's classifier confidence.
- [x] `api/routes/exceptions.py` read path (~L1501) —
      `AnalysisResponse.confidence` scales from the persisted
      0.0-1.0 float to 0-100 int with `max(0, min(100, ...))`
      clamp; missing / zero / negative values fall back to 0
      (never a fabricated mid-range default).
- [x] 4 new tests in `tests/test_analysis_confidence_persistence.py`
      — fallback-classifier confidence (0.90 → 90 for
      CONTRACTUAL_CORRECTION); no-trace records → 0 (was
      fabricated 70); malformed string → 0; out-of-range 1.5 →
      clamped to 100.
✅ Outcome: AgentReasoningCard's confidence pill is the real
classifier output, not a synthesised 80 sentinel. Closes the
partial-truth state Compliance held veto over (Pillar 2 / Guardrail
#6 in `compliance/audit_bearing_registry.yaml` parlance).

### 26.3 V005 — drop intent CHECK constraint + UUID/datetime coercion
- [x] `db/migrations/V005__drop_intent_check.sql` — drops the
      `chk_exceptions_intent` CHECK constraint that pinned the
      intent enum at the DB layer. Intent vocabulary now lives
      exclusively in `contracts/models.py::Intent`; adding a
      new intent value requires zero DB migration coordination.
- [x] `db/repository.py` row-to-dict — UUID and datetime values
      coerced to strings on read so JSON serialisation doesn't
      crash on Postgres native types.
✅ Outcome: New intents ship through `contracts/models.py` only.

### 26.4 ADR drafts (Proposed; not yet shipped)
- [x] **ADR-026** — Event-driven ingestion (Phase B) via Azure
      Event Hubs + M365 email connector + bus consumer. Status:
      Proposed. Phase B.2 documents per-node real WaterfallStepper
      timings as deferred (orchestrator emission gap —
      `WSEvent.pipeline_progress` factory exists but is uncalled).
- [x] **ADR-027** — Pipeline visualization hybrid (trace-derived
      EventsTimeline + compliance DAG). Status: Proposed (rev. 3
      — reanalysis attempt-scoping added). Reviewer chain:
      AI/LangGraph → Compliance → Tools Admin → Frontend Platform
      → Compliance veto holder.
✅ Outcome: Both ADRs route past the right reviewers before any
code moves. Architecture v4 references them as "Proposed;
absorption deferred to v4.1 when shipped."

### 26.5 Tests
Final suite: 1688 passed, 35 skipped (vs Phase 25 baseline 1592
passed, 35 skipped). +96 net new tests across §26.1 / §26.2 /
§26.3 plus an architectural lock test on the asoe-ui side asserting
every `LIVE_METHODS` entry has its `if (USE_REAL_API)` branch.

✅ Phase 26 outcome: post-deploy stabilisation. The deployed
sandbox now serves real classifier confidence values,
operator-tunable JWT TTLs, and a DB schema that no longer pins
the intent enum at two places. Two architectural decisions
(ADR-026/027) are queued for review board sign-off.

---

## Phase 26.x — Order-level enrichment composer + SoD self-override allowance (closed 2026-05-03)

Paired with asoe-ui PR #118 / #119. Both shipped together to close
gaps the PO observed on the Azure deployment.

### `api/profile_composer.py` (new module — 2026-05-03)
- [x] `compose_entity_profile(record)` — Account master-data lookup
      via `api.users.get_account` (id), with synthesis fallback
      onto `record.account_name` when the id is unknown. Returns
      `None` when no Account linkage exists.
- [x] `compose_impact_metrics(record)` — deterministic line-item
      math (delta, revenue at risk, fulfilment gap %, SLA priority
      mapped from `shadow_verdict`). Returns `None` when no line
      items so the UI omits a zero-filled column.
- [x] `compose_narrative(record, trace_data)` — order-level
      `root_cause` + `recommendation` prose, sourced from
      `record.resolution_data` first (`root_cause`,
      `root_cause_summary`, `recommendation`, `recommended_action`,
      `summary`), falling through to `trace.narrative` (first
      paragraph) and `trace.resolution_steps[0]`. Returns
      `(None, None)` when nothing is available.
- [x] All three composers wired into `routes/exceptions.get_analysis`.
- [x] 15 unit tests in `tests/test_profile_composer.py` covering
      account lookup, synthesis fallback, line-item math, fulfilment-
      gap presence rules, SLA-priority mapping, narrative source
      precedence, partial-truth guards.

### `api/schemas.py` (`AnalysisResponse` extension)
- [x] New Pydantic models `EntityProfile` and `ImpactMetrics`
      mirroring the UI contract in
      `asoe-ui/src/types/exceptions.ts`.
- [x] `AnalysisResponse` extended with optional `entity_profile`,
      `impact_metrics`, `root_cause`, `recommendation` (all four
      Optional — composer returns None when backing data absent).

### `compliance/audit_bearing_registry.yaml`
- [x] Registered all 14 new fields under `EntityProfile` (7 — 2
      audit-bearing, 5 contextual) and `ImpactMetrics` (7 — 4
      audit-bearing, 3 contextual).
- [x] Added `entity_profile_master_gap` grandfather clause for
      `vip_status` / `credit_standing` / `location` (deadline
      2026-08-01 — re-evaluate at next compliance workshop).
- [x] Added `impact_metrics_sla_gap` grandfather clause for
      `sla_deadline` (deadline 2026-08-01).
- [x] Updated `summary` tally to 124 / 92 / 5 / 27 — verified by
      `tests/test_audit_registry_coverage.py`.

### SoD self-override allowance (PO ruling 2026-05-03)
- [x] Removed the SoD self-block at `routes/exceptions.py:902-916`.
      The same user can now re-override their own prior resolution
      via `PATCH /exceptions/{id}/disposition`. Operators reported
      legitimate "I need to correct my earlier override" cases were
      forced into escalation churn.
- [x] Audit-trail evidence preserved: `reanalysis_history` records
      every override attempt with initiator / timestamp / reason_tag.
- [x] Four-eyes high-value-override rule (`POLICY_FOUR_EYES_THRESHOLD`
      → `PENDING_COSIGN` → distinct cosigner) **unchanged** —
      remains the SOX §404 control of record. The cosign self-block
      at `routes/exceptions.py:666-674` is still in force.
- [x] `tests/test_override_escalate.TestSegregationOfDuties` flipped
      the existing test to assert the new behaviour
      (`test_same_user_can_override_own_resolution`); the
      different-user override test stays unchanged.
- [x] Documented under `docs/AUDITOR_GUIDE.md §18.1` with an
      explicit scope note distinguishing the relaxed disposition
      self-block from the still-active cosign self-block.

## PHASE 27 — Case-centric architecture foundation (ADR-038 / ADR-039)

ADRs and Phase H.1 → H.7 of the case-centric rollout. See
`docs/adr/ADR-038-case-centric-order-intake.md`,
`docs/adr/ADR-039-llm-compliance-shadow-second-opinion.md`,
and `docs/plans/case-centric-rollout.md`.

**Status:** primitives shipped on main; integration and compliance
gates still pending (see "Pending" subsection at the bottom).

### 27.0 Architecture decisions (Proposed → Accepted gate pending)
- [x] ADR-038 authored (12 sections, ~1300 lines): five-layer
      architecture (L0 Knowledge / L1 Deterministic / L2 Bounded
      LLM / L3 Case Agent / L4 Harness); Manual / Automated Order
      vocabulary; channel-neutral issue/intent naming; OrderCase
      parent + correlation lookup-or-create; T1/T2/T3 case
      materialisation; single Case Agent with ~18-tool surface;
      memory hierarchy + deterministic compaction; per-tier cost
      and latency budgets ($0.001 / $0.05 / $0.08).
- [x] ADR-039 authored (8 sections, ~600 lines): constrained-output
      L2 LLM Shadow alongside the deterministic L1; **asymmetric
      combination rule** (LLM can DOWNGRADE GREEN → YELLOW but
      never UPGRADE); two gating triggers; cache discipline;
      tenant isolation; phased rollout X.1 → X.4.
- [x] `docs/plans/case-centric-rollout.md` published —
      meta-decisions, H.1 → H.7 phase plan, per-phase test strategy,
      §3.4 in-flight branch adaptation (RESOLVED in this branch
      via the email-order-entry coherence-fix commit).
- [x] `architecture_v4.md` §15 addendum — Proposed-ADR lineage
      hint + supersession map.
- [ ] **Compliance ratification — ADR-038 §7.4 (compaction
      protocol).** Status: pending workshop.
- [ ] **Compliance ratification — ADR-038 §8.5 (governance /
      CODEOWNERS map).** Status: pending workshop.
- [ ] **Compliance ratification — ADR-039 §4.1 (combination
      rule).** Status: pending workshop.
- [ ] **Compliance ratification — ADR-039 §6 (phased rollout
      X.1 → X.4).** Status: pending workshop.

### 27.1 Phase H.1 — L0 Knowledge layer foundation
- [x] `knowledge/skills/<name>/` bundle layout for all 10 skills
      (back-order-resolution, delivery-delay, duplicate-po,
      edi-mismatch, email-order-entry, moq-round-up, over-max-trim,
      pallet-alignment, price-hold-release, pricing-reconciliation).
      `git mv` preserved SKILL.md history.
- [x] `knowledge/skills/<name>/metadata.yaml` per bundle —
      schema_version 1; `recipes`, `intents`, `event_types`,
      empty `anchor_examples` / `on_demand_examples` / `assets`,
      `runtime_includes: [SKILL.md]`, token budget defaults.
- [x] Empty `examples/`, `assets/`, `specs/` directories per
      ADR-038 §5.5 (examples are *earned* by real failures, not
      authored speculatively).
- [x] `skills/loader.py` rewritten — bundle-first resolution
      via `_bundle_name_from_legacy_filename` + `_resolve`, with
      one-release legacy fallback at `skills/<name>_SKILL.md`.
- [x] `constraints/llm_backend.py::_load_skill_catalog()` walks
      `knowledge/skills/<bundle>/SKILL.md` preferentially with
      legacy fallback; alphabetical for prompt-cache stability.
- [x] `tests/test_knowledge_bundle.py` — 122 parametrized tests
      (10 bundles × layout / metadata schema / loader resolution
      / LLM-catalog walk).
- [x] §3.4 coherence-fix: `email-order-entry` migrated to bundle
      layout in the same merged PR so the H.1 invariant ("every
      skill is a bundle") holds across the combined branch.

### 27.2 Phase H.2 — `OrderCase` primitive + correlation table
- [x] `contracts/models.py` — `CaseSource` / `CaseStatus` /
      `CaseTier` Literals; `OrderCase` Pydantic model;
      `CaseCorrelationKeyType` / `CaseCorrelationKey`.
- [x] `api/store.py` — `CaseStore` class with `lookup_or_create`
      (SO → PO → EDI → email priority), `find_by_correlation`,
      `register_correlation`, `get` / `update` / `list_by_tenant`
      / `clear`; `case_store` singleton.
- [x] `ExceptionRecord.parent_case_id` added (nullable; legacy
      records remain `NULL` until backfill).
- [x] `db/migrations/V009__order_case.sql`,
      `V010__case_correlation_keys.sql`.
- [x] `tests/test_order_case.py` — 22 tests.

### 27.3 Phase H.3 — Lazy case materialisation
- [x] `api/case_resolver.py` — `derive_source_and_channel(event)`,
      `should_materialise(event, final_status)`,
      `resolve_or_open_case`, `materialise_for_event`.
- [x] Manual Orders open eagerly; Automated Orders open lazily
      on non-clean terminal status. Clean Automated COMPLETE
      records persist with `parent_case_id = None`.
- [x] `api/routes/exceptions.py::_persist_exception` calls
      `materialise_for_event` and threads `parent_case_id` into
      `exception_store.create()`.
- [x] `tests/test_e2e_case_materialisation.py` — 16 tests.

### 27.4 Phase H.4 — L2 attachment-extractor primitive
- [x] `agents/primitives/extract_attachment.py` —
      `AttachmentRef`, `ExtractedField`, `ExtractedFields`,
      `MultimodalProvider` Protocol, `StubMultimodalProvider`,
      tenant-isolated `ExtractionCache` (cache key includes
      `tenant_id` per ADR-038 §5.8), `extract_attachment()`,
      `fingerprint_for_template()`, `attachment_ref_from_metadata()`.
- [x] `tests/test_extract_attachment.py` — 15 tests.
- [ ] **Pending:** real multimodal model wired (procurement —
      Claude vision vs Azure Document Intelligence vs hybrid;
      ADR-038 §11.2 open question).

### 27.5 Phase H.5 — Case Agent loop + tool registry (primitive only)
- [x] `agents/budget.py` — `CaseBudget.for_tier(N)` with
      ADR-038 §8.1 limits (T1 4k/1k/1iter/<500ms/<$0.001;
      T2 16k/4k/6iter/<8s/<$0.05; T3 8k/2k/8iter/<12s/<$0.08);
      `is_exhausted()` returns named termination reason.
- [x] `agents/case_tools.py` — `ToolCall` / `ToolResult` Pydantic
      envelopes; `ToolSpec` / `ToolRegistry` / `ToolContext`;
      `invoke_tool()` (errors coerced to `status="error"`);
      9 wired tools delegating to existing recipes / gateways
      (no new business logic).
- [x] `agents/working_memory.py` — `SYSTEM_PROMPT`,
      `WorkingMemoryFrame`, `build_working_memory()` honouring
      §5.3 cache-discipline order.
- [x] `agents/case_agent.py` — `AgentLLMResponse` (constrained:
      `tool_call | done | escalate`), `AgentLLMProvider`
      Protocol, `StubAgentLLMProvider`, `AgentRunOutcome` Literal
      (RESOLVED / ESCALATED / AWAITING_BUYER / AWAITING_ERP /
      BUDGET_EXHAUSTED / ERROR), bounded while-loop.
- [x] `tests/test_case_agent.py` — 26 tests.
- [x] `agents/harness.py` — L4 wrapper composing every cross-
      cutting concern the inner agent loop deliberately doesn't
      own: per-case concurrency lock (`CaseLockManager.try_acquire`
      returns `None` when held; matches the future SQL
      `SELECT FOR UPDATE NOWAIT`), forward-only tier graduation
      (T1 → T2 → T3, never demote), tool-call interception
      (`ToolCallReplayLog`; agent does not write — harness does),
      compaction trigger evaluation via
      `agents.compaction.apply_compaction_if_needed` after every
      step, ADR-039 X.1 observe-only L2 LLM Shadow invocation
      stamping the verdict alongside the L1 deterministic decision.
- [x] `agents/harness.py::should_route_to_case_agent` — routing
      predicate. Returns `False` unless explicitly enabled by
      config (default off until Compliance ratifies); when enabled
      routes only `EMAIL_ORDER_ENTRY_REQUEST` (the single Manual-
      Order event type for the Phase H.5 cutover).
- [x] `tests/test_harness.py` — 18 tests covering concurrency
      lock isolation (per-case + per-tenant), tier graduation
      (forward-only; clean event = no-op), replay log isolation
      per case, end-to-end happy path, lock contention short-
      circuit, tool-trace persistence, L2 Shadow invocation +
      RED short-circuit, and the routing predicate's safe-default
      semantics.
- [ ] **Pending: deterministic graph hot-path replacement.** The
      harness is in place and the routing predicate is wired but
      `should_route_to_case_agent` returns `False` unconditionally
      until Compliance ratifies (ADR-038 §H.5 + ADR-039 §6.1
      workshop gates). The flag flip is a config-only change in
      the live deployment.
- [ ] **Pending: SQL-backed concurrency lock + replay log.** The
      in-memory `CaseLockManager` and `ToolCallReplayLog`
      faithfully model the eventual SQL behaviour
      (`SELECT FOR UPDATE NOWAIT` + a `case_events` table) but
      the persistence migration ships separately.

### 27.6 Phase H.6 — UI: `/cases` surface (asoe-ui)
- [x] **Companion repo** — see `asoe-ui/tasks.md` Phase 27.6
      for the full UI tracking. Backend-side changes recorded
      here:
- [x] `api/routes/cases.py` — `GET /api/v1/cases` (filters: source,
      status; sorted newest-first; tenant-isolated) and
      `GET /api/v1/cases/{case_id}`. RBAC mirrors exceptions
      (analyst / manager / admin / viewer / partner). Partner +
      assigned-account scoping derives in-scope cases from their
      child `ExceptionRecord` rows (case carries no `account_id`)
      with a `customer_id` fallback for just-opened Manual Order
      cases that have no child yet.
- [x] `api/store.py::ExceptionStore.list_by_case` /
      `DatabaseBackedStore.list_by_case` — children-of-case lookup
      used by the route's scoping helper.
- [x] `api/schemas.py::CaseListResponse` — `{ items, total }` shape
      matching `asoe-ui/src/lib/api.ts::casesApi.list`.
- [x] `tests/test_routes_cases.py` — 17 tests (auth + tenant
      isolation + list/filter/sort/limit + detail/missing/cross-
      tenant + partner & assigned-account scoping).

### 27.7 Phase H.7 — Compaction + SLA + backfill (partial)
- [x] `agents/compaction.py` — `CompactionTrigger.evaluate()`
      (8k tokens / 25 events / 7 days); `compact_events()`
      (deterministic per-event line; 2k token cap);
      `run_compaction()` and `replay_compaction()`.
- [x] `knowledge/compaction/__general__.template.md` — fallback
      summarisation template.
- [x] `agents/sla.py` — `SlaPolicy` / `SlaPolicySet`,
      `get_policy()` / `reload_policy()`,
      `hours_for_customer_tier()`, `stamp_sla_deadline()`.
- [x] `knowledge/policy/sla_per_customer_tier.yaml` — Strategic
      4h / Mid-Market 24h / Long-tail 72h / default 48h.
- [x] `agents/backfill.py` — `backfill_orphan_cases()` Pass 1;
      `merge_orphan_cases_by_correlation()` Pass 2 (optional,
      maintenance-window).
- [x] `db/migrations/V011__backfill_orphan_cases.sql` — Postgres
      companion with deterministic `case_id` derivation
      (`sha256(tenant||order_id)[:16]`).
- [x] `Dockerfile.api` — `COPY knowledge/` and `COPY agents/`
      so runtime-loaded resources ship.
- [x] `tests/test_compaction_sla_backfill.py` — 30 tests
      (27 original + 3 for `apply_compaction_if_needed`).
- [x] `agents/compaction.py::apply_compaction_if_needed` —
      imperative wrapper around `run_compaction()` that also
      persists `working_memory_summary` + `last_compaction_at`
      on the case row. Phase H.5 / H.7 callers can fire-and-
      forget; pure `run_compaction()` is retained for harness
      sites that own their own persistence.
- [x] `scripts/run_backfill.py` — ops CLI wrapping
      `backfill_orphan_cases()` (Pass 1) and
      `merge_orphan_cases_by_correlation()` (Pass 2). Supports
      `--pass {1,2,both}`, `--dry-run`, `--tier-map FILE`
      (JSON customer→tier map for SLA stamping), and a custom
      `--bundle-version-at-open` sentinel. Emits a JSON report
      to stdout. Exit codes: 0 success / 2 invalid args / 3
      runtime error.
- [x] `tests/test_run_backfill_script.py` — 9 tests covering
      pass selection, idempotency, dry-run, tier-map loading,
      and arg validation.
- [ ] **Pending: per-event-type compaction templates.** Only
      `__general__.template.md` exists. ADR-038 §11.2 open
      question (Compliance + domain SME).
- [ ] **Pending: agent loop / harness wire-up of
      `apply_compaction_if_needed`.** The helper exists but
      the L4 harness / Case Agent loop that should call it on
      every step boundary is the Thread 4 work (Phase H.5
      agent dormant).
- [ ] **Pending: four-eyes / cosign / override flows migrated
      to operate on the case lifecycle** (rather than the
      exception lifecycle in isolation, as ADR-029 currently
      does). Phase H.7 closeout requirement.
- [ ] **Pending: scheduled-job wrapping of
      `scripts/run_backfill.py`.** The CLI is shipped; ops
      still has to register it as a cron / k8s CronJob
      (deployment-side, not code-side).

### 27.8 ADR-039 — L2 LLM Shadow (X.1 primitive shipped; harness wire-up pending)
The X.1 observe-only **primitive** is now in place. The harness
wire-up that actually invokes it from `shadow_audit` is intentionally
held back to Thread 4 (so the agent + harness extensions land
together). Verdicts produced today are not yet attached to any
`ComplianceDecision` until Thread 4 ships.
- [x] `compliance/shadow_llm.py` — constrained-output L2 Shadow
      primitive (`AGREE | DISAGREE_DOWNGRADE | ABSTAIN`) with
      `LLMShadowProvider` Protocol, `StubLLMShadowProvider`
      (deterministic stand-in until procurement), per-tenant
      `ShadowLLMCache` (24h TTL, SHA-256 key over
      `tenant_id || bundle_version || model_id || canonical(request)`),
      gating logic per §5.2, and out-of-vocab concern dropping.
- [x] `knowledge/shadow_llm/` bundle — `system_prompt.md`,
      `concerns_vocabulary.yaml` (12 seed concerns), `metadata.yaml`
      (rollout config: `current_phase: X.1`,
      `financial_impact_threshold_usd: null` = observe-only),
      empty `few_shot_examples/` (earned, not authored).
- [x] `contracts/models.py::ShadowLLMVerdict` — Pydantic model with
      `model_config = ConfigDict(extra="forbid")`. Closed-Literal
      `action` enum (no `DISAGREE_UPGRADE` — asymmetric authority
      is enforced **in the schema**). `reason` length-capped at 200
      chars, `confidence` bounded `[0.0, 1.0]`, `policy_concerns`
      typed `List[str]`.
- [x] `contracts/models.py::ComplianceDecision.llm_shadow_verdict`
      — new optional field. `None` when L2 was skipped (gating /
      RED short-circuit / provider failure); populated when invoked.
- [x] `contracts/models.py::LLMCallTrace.task` — Literal extended
      with `"shadow_llm"` so audit queries separate L1 deterministic
      from L2 LLM telemetry.
- [x] SLI counters per §7.3 — `compliance.shadow_llm.shadow_llm_metrics`
      module-level `ShadowLLMMetrics` (invocations total / by trigger,
      cache hits, verdicts by action, timeout / unavailable /
      validation-error counters, latency sum + count, cost). Thread 4
      will wire these into the `/api/v1/health/metrics` Prometheus
      surface.
- [x] `tests/test_shadow_llm.py` — 27 tests covering schema
      invariants (`DISAGREE_UPGRADE` rejected, length / range bounds),
      bundle loading, gating triggers / short-circuits, stub provider
      behaviour, cache hits + tenant isolation + TTL, out-of-vocab
      concern dropping, all three provider failure modes, and SLI
      counters.
- [x] L4 harness `shadow_audit` wire-up (Compliance ratified post-
      merge). `orchestration/nodes.py::shadow_audit` now invokes
      the L2 Shadow after the deterministic gate; verdict stamped
      onto `state.shadow.llm_shadow_verdict`; `LLMCallTrace`
      task='shadow_llm' appended. **X.1 invariant preserved:** the
      L2 verdict does NOT move `state.shadow.status` or
      `state.final_status` — that's X.2+ behaviour and lands
      behind a separate gate. Kill switch:
      `ASOE_SHADOW_LLM_DISABLED=1`. Tenant id now plumbed onto
      `GraphState.tenant_id` (load-bearing for ADR-038 §5.8 /
      ADR-039 §5.5 cache key isolation).
- [x] `tests/test_shadow_audit_l2_wireup.py` — 10 tests covering
      RED short-circuit, YELLOW always-invokes, GREEN floor
      gating, observe-only status invariant, LLMCallTrace
      append, kill-switch, tenant propagation to cache.
- [ ] **Pending §8.1 procurement gates** (model choice — Haiku vs
      local Ollama vs hybrid; first earned anchor examples).
- [ ] **Pending compliance ratification gates** §4.1 (combination
      rule) and §6 (phased rollout) — required before any X.2
      verdict-affecting deployment.
- [ ] X.2 / X.3 / X.4 — all blocked on X.1 telemetry collection +
      the compliance ratification gates above.

### 27.9 Phase 27 follow-ups (deferred from the merged PR)
- [x] **Spec relocation** —
      `docs/specs/order-entry-from-email-product-spec.md` →
      `knowledge/skills/email-order-entry/specs/order_entry_spec.md`
      via `git mv` (history preserved). 4 reference paths
      updated in the same commit
      (`recipes/EmailOrderEntryRecipe.py`, `contracts/policy.py`,
      `docs/adr/ADR-034-email-order-entry-skill.md`,
      `docs/plans/case-centric-rollout.md`).
- [ ] **Per-skill anchor example earning** — all 10 bundles
      ship `anchor_examples: []` per §5.5 ("earned, not
      authored"). The first earnings cycle starts when Phase
      H.5 routes real traffic through the agent.
- [ ] **`MANUAL_ORDER_INTAKE` rename of `EMAIL_ORDER_ENTRY`** —
      optional channel-neutral cleanup (§3.2 binding); deferred
      until Phase H.5 wire-up.
- [ ] **`architecture_v5.md` draft** — the rollout plan
      promises v5 once ADR-038/039 are *Accepted* and Phase
      H.1 has shipped. Code is ahead but ratification gates
      above are still pending; drafting v5 before ratification
      would cement Proposed-status decisions. Defer until the
      compliance workshops complete.
