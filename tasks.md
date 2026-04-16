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
