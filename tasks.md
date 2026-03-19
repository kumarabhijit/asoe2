# tasks.md
## Project: CPG Agentic Pricing Exception System
## Phase-Based Incremental Build Plan
---
## PHASE 0 — Foundation (NO BUSINESS LOGIC)
### 0.1 Repository Structure
- [ ] Create folders:
  - `contracts/`
  - `skills/`
  - `recipes/`
  - `orchestration/`
  - `compliance/`
  - `constraints/`
  - `mcp/`
  - `tests/`
  - `prompts/`
- [ ] Move existing recipes into `recipes/`
- [ ] Move SKILL.md into `skills/`
✅ Outcome: clean separation of brain vs muscle
---
### 0.2 Type Contracts
- [ ] Define Pydantic models for:
  - OrderEvent
  - PricingDiscrepancy
  - RecipeInvocation
  - ComplianceDecision
  - ExecutionLog
  - GraphState
- [ ] Define constrained output schemas for Guidance / Outlines:
  - IntentDecision
  - ShadowDecision
  - RecipeProposal
- [ ] Enforce strict validation before execution
✅ Outcome: no untyped execution paths
---
## PHASE 1 — Skill Loading & Reasoning
### 1.1 Skill Loader
- [ ] Implement dynamic loader for `skills/*.md`
- [ ] Load skills **only when relevant**
- [ ] Ensure skill text is injected verbatim (no summarization)
✅ Outcome: progressive disclosure works as designed
---
### 1.2 Intent Classifier (Non-Executing)
- [ ] Given an OrderEvent:
  - Identify price gap
  - Classify intent:
  - `CONTRACTUAL_CORRECTION`
  - `CREDIT_BLOCK`
  - `MASS_PRICING_ERROR`
  - `DUPLICATE_PO`
- [ ] Constrain intent output vocabulary using Guidance / Outlines
- [ ] Output intent + confidence
- [ ] NO recipe calls yet
✅ Outcome: reasoning-only stage is testable
---
## PHASE 2 — Compliance Shadow
### 2.1 Shadow Interface
- [ ] Define ComplianceShadow API contract
- [ ] Implement stub that returns:
  - `GREEN`
  - `YELLOW`
  - `RED`
- [ ] Constrain shadow verdict using Guidance / Outlines
- [ ] Log shadow decision with TraceID
- [ ] Include `TraceID`, reasons, policy hits
✅ Outcome: compliance is always first-class
---
### 2.2 Shadow Enforcement
- [ ] Block execution on `RED`
- [ ] Force explanation output on `RED`
- [ ] Route `YELLOW` to `MANUAL_REVIEW_REQUIRED`
- [ ] Allow auto-proceed only on `GREEN`
✅ Outcome: no silent violations
---
## PHASE 3 — Recipe Invocation
### 3.1 Recipe Registry
- [ ] Register recipes with:
  - Name
  - Required parameters
  - Allowed intents
- [ ] Reject unknown recipe calls
✅ Outcome: zero dynamic execution
---
### 3.2 Deterministic Execution Wrapper
- [ ] Execute recipe via subprocess or function call
- [ ] Constrain recipe proposal to registered recipe names using Guidance / Outlines
- [ ] Capture:
  - Inputs
  - Outputs
  - Errors
- [ ] Return immutable execution log
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
- [ ] Track execution counts
- [ ] Enforce:
  - Max updates / window e.g. max 50 pricing updates / 5-minute window
  - Max financial exposure e.g. max $10,000 total dollar variance per batch
- [ ] Route to HITL / `FAIL_TO_HUMAN` on breach
✅ Outcome: systemic risk control
---
## PHASE 5 — Observability & Tests
### 5.1 LangFuse-Ready Integration/Tracing
- [ ] Trace:
  - Skill used
  - intent selected
  - Shadow verdict
  - Recipe output
  - RAG chunks
  - TraceID
- [ ] Keep implementation self-host ready
### 5.2 Golden Tests
- [ ] Test each intent → recipe mapping
- [ ] Test shadow rejection paths
- [ ] Test FAIL_TO_HUMAN paths
- [ ] Test constrained output schemas and allowed vocabularies
✅ Outcome: regression-proof system
---
## PHASE 6 — Hardening
- [ ] Kill switch config
- [ ] Read-only “explain mode”
- [ ] Documentation for auditors
- [ ] Document Guidance / Outlines safeguards for downstream systems
✅ Outcome: production readiness
---
## PHASE 7 — Infrastructure Gateways & Multi-Step Workflows
### 7.1 Infrastructure Gateway Layer (Hexagonal Architecture)
- [ ] Define `InfrastructureGateway` protocol (Port) in `gateways/base.py`
- [ ] Implement Gateway Registry (`register_gateway`, `get_gateway`, `clear_registry`)
- [ ] Implement `GatewayExecutor` with structured tracing and error handling
- [ ] Implement `StubGateway` test double (canned responses, call recording)
- [ ] Add typed contracts: `GatewayRequest`, `GatewayResponse`, `GatewayDependency`, `GatewayEffect`
- [ ] Extend `RecipeSpec` with optional `dependencies` and `effects` tuples
- [ ] Add `resolve_dependencies` node (pre-recipe gateway data resolution)
- [ ] Add `apply_effects` node (post-recipe gateway side effect application)
- [ ] Wire new nodes into graph: `validate_types → resolve_dependencies → execute_recipe → apply_effects → END`
- [ ] Add `gateway_calls` field to `TraceRecord` for observability
✅ Outcome: recipes stay pure; infrastructure I/O is decoupled via Ports & Adapters

### 7.2 Multi-Step Workflow Runner (Saga Pattern)
- [ ] Define typed contracts: `WorkflowStep`, `WorkflowDefinition`, `WorkflowStepResult`, `WorkflowResult`
- [ ] Implement `WorkflowRunner.run()` — sequential step execution through full graph
- [ ] Implement Saga compensation — LIFO reverse through completed steps on failure
- [ ] Support `input_mapping` — carry state forward between steps
- [ ] `WorkflowResult.status`: `COMPLETE`, `FAILED`, `COMPENSATED`, `PARTIAL`
- [ ] Each step runs through full compliance shadow independently
✅ Outcome: multi-intent workflows with compensation; each step fully audited

### 7.3 DUPLICATE_PO Fallback Backend
- [ ] Add `DUPLICATE_PO` classification branch in `DeterministicFallbackBackend.classify_intent()`
- [ ] Add `DUPLICATE_PO → DuplicatePORecipe.py` mapping in `propose_recipe()`
✅ Outcome: DUPLICATE_PO intent is fully routable end-to-end in CI/test mode
---
## PHASE 8 — Local Execution Sandbox
### 8.1 SQLite Seeder (`tests/sandbox/seed.py`)
- [x] Define SQLite schema: `sap_pricing`, `retailer_contracts`, `credit_profiles`, `edi_events`
- [x] Seed 5 SKUs, 7 retailer contracts, 4 credit profiles
- [x] Seed 8 EDI events covering all four intents (CONTRACTUAL_CORRECTION ×3, CREDIT_BLOCK ×2, MASS_PRICING_ERROR ×1, DUPLICATE_PO ×2)
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
