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
  - ~~`mcp/`~~ (deferred — MCP integration is stubbed per architecture_v2.md §2; directory not created)
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

### 8.5 Sandbox Dependencies (`tests/sandbox/requirements-sandbox.txt`)
- [x] `streamlit>=1.35.0`
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
- [x] `uv` for fast, deterministic dependency resolution (per architecture_v2.md §2)
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
✅ Outcome: deployment manifests align with architecture_v2.md §2 infrastructure stack
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
