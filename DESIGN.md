# Design Reference — CPG Agentic Pricing Exception System

**Implements:** [architecture_v3.md](architecture_v3.md) (patterns, principles, technology choices)
**Audience:** Active developers working in this codebase
**Purpose:** Maps architectural patterns to concrete modules, classes, functions, and wiring

---

## 1. Module Structure

```
contracts/
  models.py          # Pydantic contracts: OrderEvent, GraphState, RecipeInvocation, etc.
  policy.py          # Externalized business thresholds (single source of truth)

skills/
  loader.py          # Dynamic SKILL.md loader (progressive disclosure)
  intent_classifier.py  # Intent classification logic
  *.md               # Skill definition files (loaded verbatim, never summarised)

recipes/
  PriceAdjustmentRecipe.py      # CONTRACTUAL_CORRECTION / MASS_PRICING_ERROR
  CreditHoldReleaseRecipe.py    # CREDIT_BLOCK
  DuplicatePORecipe.py          # DUPLICATE_PO
  PriceHoldReleaseRecipe.py     # PRICE_HOLD_RELEASE — EDI 850 pricing-block disposition
  EdiMismatchRecipe.py          # EDI_MISMATCH — SKU/QTY/UOM/SHIP_TO sub_type classification
  BackOrderResolutionRecipe.py  # BACK_ORDER — OOS gap classification + ranked resolution options
  OverMaxTrimRecipe.py          # OVER_MAX — per-line trim plan for contract-max exceedance
  MOQRoundUpRecipe.py           # MIN_ORDER_QTY — round-up / accept-below / escalate decision
  PalletAlignmentRecipe.py      # PALLET_CONFIG — broken-layer / partial-pallet alignment
  DeliveryDelayResolutionRecipe.py # DELIVERY_DELAY — severity + ranked expedite/split/reschedule
  registry.py        # Recipe registry (name → spec mapping)
  executor.py        # RecipeExecutor — dispatches to registered recipes

compliance/
  shadow.py          # ComplianceShadow: audit() → ComplianceDecision, enforce() → ShadowEnforcement

constraints/
  specs.py           # Constrained output schemas: IntentDecision, ShadowDecision, RecipeProposal
  router.py          # ConstraintRouter — per-task backend selection (ASOE_LLM_PROVIDER + per-task overrides + ASOE_LLM_DISABLE_FOR + kill-switch + explain-mode gates)
  fallback_backend.py    # DeterministicFallbackBackend (no LLM, CI/test)
  outlines_backend.py    # OutlinesConstrainedBackend (production Outlines regex)
  guidance_backend.py    # GuidanceRegexBackend (regex patterns for Guidance / Outlines)
  llm_backend.py     # RemoteLLMBackend — provider-agnostic constraint backend (composes any LLMProviderClient + sanitiser + budget + circuit breaker; falls through to deterministic on failure)
  cross_check.py     # cross_check() — compares LLM vs deterministic intent; disagreement → MANUAL_REVIEW_REQUIRED

llm/
  backends.py        # get_outlines_model() — cached Outlines + HuggingFace model loader (used by OutlinesConstrainedBackend)
  provider_protocol.py    # LLMProviderClient Protocol + ToolCallResult / SystemBlock / CacheControl / TokenUsage / ProviderError dataclasses (provider-agnostic surface)
  provider_factory.py     # PROVIDER_FACTORIES registry + build_provider_client(provider)
  anthropic_client.py     # AnthropicProviderClient — direct API or Azure AI Foundry private endpoint
  openai_client.py        # OpenAIProviderClient — OpenAI direct, Azure OpenAI, or OpenAI-compatible (vLLM/TGI/LiteLLM, including Qwen on a vLLM cluster)
  ollama_client.py        # OllamaProviderClient — self-hosted or Cloud (Qwen2.5+, Llama 3.1+, Mistral)
  huggingface_client.py   # HuggingFaceProviderClient — Dedicated Inference Endpoints + Serverless Inference API
  google_client.py        # GoogleProviderClient — Vertex AI / Gemini (V1 stub)
  sanitizer.py            # OrderEvent.metadata allowlist + length-cap + untrusted-data delimiter for LLM prompts (Chen review §5 mitigation)
  budget.py               # InMemoryBudgetTracker + RedisBudgetTracker — daily USD spend cap with soft-warn / hard-block thresholds
  circuit_breaker.py      # LLM-tier circuit breaker — sliding 60s window, error-rate / p95-latency trip, HALF_OPEN probe semantics (separate from $10k batch breaker)

orchestration/
  graph.py           # LangGraph graph builder: build_graph(), build_explain_graph()
  nodes.py           # Graph node functions (one per state transition)
  utils.py           # circuit_breaker(), helper utilities

gateways/
  base.py            # InfrastructureGateway Protocol (Port)
  registry.py        # Gateway registry: register_gateway(), get_gateway(), clear_registry()
  executor.py        # GatewayExecutor — wraps calls with tracing, timeout, error handling
  stub.py            # StubGateway — test double (canned responses, call recording)

workflows/
  runner.py          # WorkflowRunner — multi-step Saga execution with LIFO compensation

hardening/
  kill_switch.py     # is_kill_switch_active(), apply_kill_switch()
  explain_mode.py    # is_explain_mode_active(), build_explain_summary()

observability/
  tracer.py          # TraceRecord (Pydantic, LangFuse-aligned), emit via stdlib logging

api/
  app.py             # FastAPI application factory (create_app())
  deps.py            # Auth dependencies: JWT validation, RBAC, tenant extraction
  errors.py          # Standard error envelope (ASOEError, ErrorEnvelope)
  schemas.py         # Request/Response Pydantic models
  store.py           # Exception store: in-memory (default) or DatabaseBackedStore (when DATABASE_URL set)
  users.py           # User store (6 seed users), Account entity (4 accounts), compute_visible_tabs(), expand_permissions()
  events.py          # WSEvent schema + factory methods (pipeline_progress, task_complete, etc.)
  pubsub.py          # InMemoryPubSub / RedisPubSub, create_pubsub() factory, event_publisher singleton
  routes/
    health.py        # GET /api/v1/health (public, no auth)
    exceptions.py    # Exception CRUD + resolve endpoints (11 routes)
    accounts.py      # GET /api/v1/accounts — account list with account scoping
    workflows.py     # POST /api/v1/workflows
    policies.py      # PUT /api/v1/policies/{tenant_id}
    auth.py          # Auth endpoints: login, SSO, MFA, refresh, me, switch (sandbox), users list (sandbox)
    ws.py            # WebSocket hub — ws://host/api/v1/ws (§10)

db/
  connection.py      # SQLiteAdapter / PostgresAdapter, create_adapter() factory
  repository.py      # ExceptionRepository, TraceRepository, PolicyRepository
  migrations/
    runner.py        # apply_migrations() — SQLite or PostgreSQL
    V001__initial_schema.sql  # PostgreSQL schema (5 tables, indexes, RLS, triggers)

tests/
  conftest.py        # Shared fixtures (StubGateway, sample events, backend setup)
  test_*.py          # Unit and integration tests (run `python -m pytest` to verify)
  sandbox/           # Interactive exploration tools (see §16)
```

### Key Schemas (`contracts/models.py`)

**`GraphState`** (`extra="forbid"` — no untyped fields allowed):

| Field | Type | Populated By |
|---|---|---|
| `event` | `OrderEvent` | Caller |
| `discrepancy` | `Optional[PricingDiscrepancy]` | `classify` |
| `rag_context` | `RagContext` | Reserved for V2 |
| `skill` | `Optional[SkillDocument]` | `load_skill` |
| `intent` | `Intent` | `classify` |
| `confidence` | `float` | `classify` |
| `shadow` | `Optional[ComplianceDecision]` | `shadow_audit` |
| `selected_recipe` | `Optional[str]` | `select_recipe` |
| `invocation` | `Optional[RecipeInvocation]` | `validate_types` |
| `execution_log` | `Optional[ExecutionLog]` | `execute_recipe` |
| `final_status` | `Optional[TerminalStatus]` | Any node (on halt/completion) |
| `explanation` | `Optional[str]` | Auto-populated at terminal state |
| `update_count` | `int` | `ingest` |
| `batch_total_variance` | `float` | `ingest` |
| `resolved_data` | `Dict[str, Any]` | `resolve_dependencies` |
| `effect_results` | `List[GatewayResponse]` | `apply_effects` |

See architecture_v3.md §5.2 for `OrderEvent` and `ExecutionLog` field-level schemas.

### Recipe Specs (`recipes/registry.py`)

Each recipe declares a `RecipeSpec` that the orchestration layer uses to validate params, resolve gateway dependencies, and apply effects.

**PriceAdjustmentRecipe.py:**
- Allowed intents: `CONTRACTUAL_CORRECTION`, `MASS_PRICING_ERROR`
- Required params: `order_id`, `line_item`, `po_price`, `sap_base_price`, `max_discount_allowed`, `price_condition_type`
- Dependencies: _(none in V1 — pricing data arrives in OrderEvent)_
- Effects: _(none in V1 — SAP write-back stubbed)_

**CreditHoldReleaseRecipe.py:**
- Allowed intents: `CREDIT_BLOCK`
- Required params: `order_id`, `requester_role`, `credit_limit`, `current_exposure`, `authorized_roles`, `exposure_tolerance`
- Dependencies / Effects: _(none in V1)_

**DuplicatePORecipe.py:**
- Allowed intents: `DUPLICATE_PO`
- Required params: `order_id`, `po_number`, `customer_id`, `signal_scores`, `threshold_auto_block`, `threshold_review_required`, `threshold_soft_flag`, `autonomy_levels`
- Dependencies: `get_fulfillment_status` (OMS gateway), `get_matched_po_details` (OMS gateway)
- Effects: `buyer_notification` (notification gateway)
- Resolution actions: `BLOCK_AND_NOTIFY`, `MERGE`, `SUPERSEDE`, `ALLOW_BOTH`, `ESCALATE`, `REQUEST_BUYER_CONFIRMATION`

**PriceHoldReleaseRecipe.py:**
- Allowed intents: `PRICE_HOLD_RELEASE`
- Required params: `order_id`, `line_item`, `po_price`, `sap_base_price`, `tolerance_pct`, `hard_block_pct`, `hold_status`
- Dependencies: `get_price_hold_status` (OMS gateway)
- Effects: `update_hold_flag` (OMS gateway)
- Recipe actions (constrained via `AllowedPriceHoldAction`): `AUTO_RELEASE` (variance within tolerance), `ESCALATE` (variance above tolerance, within hard-block), `HARD_BLOCK` (variance above hard-block)

**EdiMismatchRecipe.py:**
- Allowed intents: `EDI_MISMATCH`
- Required params: `order_id`, `sub_type`, `expected_value`, `received_value`, `autonomy_levels`
- Dependencies: none (pure classification — no I/O)
- Effects: `buyer_notification` (notification gateway)
- Accepted sub_types (constrained via `AllowedEdiMismatchSubType`): `SKU_MISMATCH`, `QTY_MISMATCH`, `UOM_MISMATCH`, `SHIP_TO_MISMATCH`. `PRICE_MISMATCH` is intentionally excluded — routed to `CONTRACTUAL_CORRECTION` / `PriceAdjustmentRecipe.py` at classifier time to preserve the pricing single-source-of-truth (CLAUDE.md §1).
- Classification vocabulary (constrained via `AllowedEdiMismatchClassification`): `HARD_REJECT`, `REVIEW`, `ESCALATE`

---

## 2. Constraint Backend Chain

The `ConstraintRouter` in `constraints/router.py` exposes
`get_constrained_backend(task: LLMTask | None = None)` and selects a
per-task backend using this resolution order:

```
0. ASOE_KILL_SWITCH=1     → DeterministicFallbackBackend (no TCP)
0. ASOE_EXPLAIN_MODE=1    → DeterministicFallbackBackend (no paid LLM)
1. task ∈ ASOE_LLM_DISABLE_FOR → DeterministicFallbackBackend
2. ASOE_LLM_PROVIDER_<TASK>    (per-task override)
3. ASOE_LLM_PROVIDER           (global default)
4. USE_OUTLINES_BACKEND=1      (legacy short-circuit → outlines)
5. fallback                    (DeterministicFallbackBackend)
```

The router NEVER raises — every failure mode falls closed to
`DeterministicFallbackBackend` with a structured warning so a
provider misconfiguration cannot crash a graph run.

**Provider matrix** (V1 PR-1):

| Provider key   | Implementation        | Hosting / Notes |
|---|---|---|
| `anthropic`    | `AnthropicProviderClient`    | Anthropic API direct OR Azure AI Foundry (set `ANTHROPIC_BASE_URL`). `claude-sonnet-4-6` default. |
| `openai`       | `OpenAIProviderClient`       | OpenAI direct, Azure OpenAI, or any OpenAI-compatible endpoint (vLLM, TGI, LiteLLM, LocalAI, Anyscale, Fireworks, Together, Groq). Self-hosted Qwen on vLLM uses this. |
| `google`       | `GoogleProviderClient`       | V1 stub. Wires to Vertex AI / Gemini in a follow-up. |
| `ollama`       | `OllamaProviderClient`       | Self-hosted (Qwen2.5+, Llama 3.1+, Mistral) or Cloud. OpenAI-compatible tool calling. |
| `huggingface`  | `HuggingFaceProviderClient`  | HF Dedicated Inference Endpoints (production) or Serverless Inference API (sandbox-only). |
| `outlines`     | `OutlinesConstrainedBackend` | Local in-process constrained generation. |
| `local`        | (loaded via `LOCAL_LLM_BACKEND_CLASS`) | Sandbox SLM plug-in. |
| `fallback`     | `DeterministicFallbackBackend` | Always-available rule engine. Default. |

**`RemoteLLMBackend`** (`constraints/llm_backend.py`) is the
provider-agnostic layer that wraps any `LLMProviderClient`. The trio
methods (`classify_intent` / `propose_recipe` / `shadow_decision`)
each go through:

```
sanitiser  → strips OrderEvent.metadata to allowlisted, length-capped
             view (Chen review §5: prompt-injection mitigation)
breaker    → acquire() before; record_success/failure after
budget     → snapshot() before (hard_block short-circuits to
             fallback); consume() after with cost from policy
             pricing table
provider   → vendor-agnostic call_with_tool() with a Pydantic-
             derived input_schema (sorted keys for cache stability)
```

On `ProviderError`, `CircuitOpen`, budget hard-block, Pydantic
validation error, or any unexpected exception → `RemoteLLMBackend`
delegates to the injected `fallback_backend` (default:
`DeterministicFallbackBackend`) for that single trio call. The graph
never sees a remote-LLM failure — it sees a normal
`IntentDecision`/`RecipeProposal`/`ShadowDecisionSchema`.

**Cross-check on intent** (`constraints/cross_check.py`): when an
LLM-backed classifier is active, the orchestration `classify` node
runs the deterministic classifier in parallel and routes to
`MANUAL_REVIEW_REQUIRED` on disagreement. Conservative shakeout
posture per CLAUDE.md §5.

**Cache strategy:** the cacheable system prompt is the verbatim
`skills/*.md` catalog (~5k tokens, alphabetically concatenated)
plus a per-task directive — both marked
`CacheControl(enabled=True)`. Per-call volatile content lives in
the user message AFTER the cached prefix.
`tests/test_llm_cache_invalidators.py` is a CI guard against
state-derived data leaking into the prefix.

**Hardening:** `ASOE_KILL_SWITCH=1` short-circuits the router AND
each provider's `from_config()` so no TCP socket is opened.
`ASOE_EXPLAIN_MODE=1` pins all tasks to deterministic so dry-runs
never incur paid LLM calls.

If `OutlinesConstrainedBackend` fails to initialise (missing
`outlines` package), the router degrades to
`DeterministicFallbackBackend` with a `logger.warning()`.

**Fallback observability:** Every backend invocation records which tier actually served the request. Fallback activations are surfaced as:
- A `backend_fallback` field in the `TraceRecord` (value: `"custom"`, `"outlines"`, or `"deterministic_fallback"`)
- The `ExecutionLog.constrained_outputs` map includes the backend tier used (e.g., `"intent" → "IntentDecision:DeterministicFallbackBackend"`)
- A `logger.warning()` on every degradation event, including the reason
- A Prometheus counter `asoe_backend_fallback_total{tier="deterministic_fallback"}` for alerting on sustained degradation
- TraceRecords where `backend_fallback == "deterministic_fallback"` are flagged with `is_fallback_generated: true` and **excluded from V2 fine-tuning datasets**

**Constrained output types** (`constraints/specs.py`):

| Schema | Constrained field | Allowed values |
|---|---|---|
| `IntentDecision` | `AllowedIntent` | `CONTRACTUAL_CORRECTION`, `CREDIT_BLOCK`, `MASS_PRICING_ERROR`, `DUPLICATE_PO`, `PRICE_HOLD_RELEASE`, `EDI_MISMATCH`, `BACK_ORDER`, `OVER_MAX`, `MIN_ORDER_QTY`, `PALLET_CONFIG`, `DELIVERY_DELAY` |
| `ShadowDecision` | `AllowedShadowStatus` | `GREEN`, `YELLOW`, `RED` |
| `RecipeProposal` | `AllowedRecipeName` | `PriceAdjustmentRecipe.py`, `CreditHoldReleaseRecipe.py`, `DuplicatePORecipe.py`, `PriceHoldReleaseRecipe.py`, `EdiMismatchRecipe.py`, `BackOrderResolutionRecipe.py`, `OverMaxTrimRecipe.py`, `MOQRoundUpRecipe.py`, `PalletAlignmentRecipe.py`, `DeliveryDelayResolutionRecipe.py` |
| _(recipe output)_ | `AllowedResolutionAction` | `BLOCK_AND_NOTIFY`, `MERGE`, `SUPERSEDE`, `ALLOW_BOTH`, `ESCALATE`, `REQUEST_BUYER_CONFIRMATION` |
| _(EdiMismatchRecipe input)_ | `AllowedEdiMismatchSubType` | `SKU_MISMATCH`, `QTY_MISMATCH`, `UOM_MISMATCH`, `SHIP_TO_MISMATCH` (PRICE_MISMATCH routed out at classifier time) |
| _(EdiMismatchRecipe output)_ | `AllowedEdiMismatchClassification` | `HARD_REJECT`, `REVIEW`, `ESCALATE` |
| _(PriceHoldReleaseRecipe output)_ | `AllowedPriceHoldAction` | `AUTO_RELEASE`, `ESCALATE`, `HARD_BLOCK` |

---

## 3. Graph Node Wiring

### Normal mode (`build_graph()`)

```
ingest → classify → load_skill → validate_circuit_breaker → shadow_audit
  → select_recipe → validate_types → resolve_dependencies → execute_recipe
  → apply_effects → END
```

### Explain mode (`build_explain_graph()`)

Same pipeline, but `execute_recipe` is replaced by `explain_only`:

| Node | Normal | Explain |
|---|---|---|
| `ingest` | runs | runs |
| `classify` | runs | runs |
| `load_skill` | runs | runs |
| `validate_circuit_breaker` | runs | runs |
| `shadow_audit` | runs | runs (real verdict) |
| `select_recipe` | runs | runs |
| `validate_types` | runs | runs |
| `resolve_dependencies` | runs | **skipped** |
| `execute_recipe` | runs | **replaced** by `explain_only` |
| `apply_effects` | runs | **skipped** |

Node functions live in `orchestration/nodes.py`. Each reads current `GraphState` and returns a partial state update.

### Autonomy-Level Routing in `execute_recipe`

The `execute_recipe` node reads the `autonomy_level` from the recipe result and branches:

| Autonomy Level | Routing |
|---|---|
| **L1** (Observe) | → `MANUAL_REVIEW_REQUIRED` — agent flags the exception, takes no action |
| **L2** (Recommend) | → `MANUAL_REVIEW_REQUIRED` — agent recommends resolution, human must approve |
| **L3** (Act & Inform) | → `COMPLETE` — agent auto-executes, notifies human post-action |
| **L4** (Full Autonomy) | → `COMPLETE` — agent auto-executes silently, logs for audit |

Default autonomy per resolution action is defined in `policy.DUPLICATE_PO_AUTONOMY_LEVELS`. See architecture_v3.md §5.8 for the full mapping.

---

## 4. Compliance Shadow Implementation

`compliance/shadow.py` provides:

- `ComplianceShadow.audit(proposal)` → `ComplianceDecision` (status, reasons, policy_hits)
- `ComplianceShadow.enforce(decision)` → `ShadowEnforcement` (action: proceed / escalate / halt)

Both methods emit structured log records to the `asoe.compliance` logger:

| Field | Source |
|---|---|
| `trace_id` | `ComplianceDecision.trace_id` |
| `status` | `GREEN` / `YELLOW` / `RED` |
| `reasons` | List of human-readable reason strings |
| `policy_hits` | List of policy identifiers that fired |
| `constrained_by` | Schema name used to constrain the verdict |

Enforcement log level: `INFO` for `GREEN`, `WARNING` for `YELLOW` / `RED`.

---

## 5. Gateway Layer (Hexagonal Architecture)

Implements the Hexagonal Gateway pattern from architecture_v3.md §7G.

| Component | File | Role |
|---|---|---|
| Protocol (Port) | `gateways/base.py` | `InfrastructureGateway` typed interface |
| Registry | `gateways/registry.py` | Maps gateway names → adapter instances |
| Executor | `gateways/executor.py` | Wraps calls with tracing + timeout enforcement |
| Stub (Test) | `gateways/stub.py` | Canned responses, call recording, no network |

**Timeout enforcement:** `GatewayExecutor` uses `concurrent.futures.ThreadPoolExecutor` to enforce `GatewayRequest.timeout_ms`. A gateway exceeding its deadline receives a `TIMEOUT` response — never an infinite hang.

**Exception handling:** Two tiers — known types (`RuntimeError`, `ValueError`, `TypeError`, `KeyError`) logged at `ERROR`; unexpected types logged at `CRITICAL` with `error_type` in the structured payload.

**Typed contracts** (`contracts/models.py`):
- `GatewayRequest`, `GatewayResponse` (response statuses: `SUCCESS`, `FAILED`, `TIMEOUT`, `UNAVAILABLE`)
- `GatewayDependency`, `GatewayEffect`
- `RecipeSpec.dependencies` and `RecipeSpec.effects` — typed tuples declared per recipe

**Graph integration:**
- `resolve_dependencies` node — reads `RecipeSpec.dependencies`, calls gateways, stores results in `GraphState.resolved_data`. Failure → `FAIL_TO_HUMAN`.
- `apply_effects` node — reads `RecipeSpec.effects`, calls gateways, stores results in `GraphState.effect_results`. Failure is logged but does not undo the recipe result.

All gateway calls are logged to `asoe.gateways` with `trace_id` correlation.

---

## 6. Workflow Runner (Saga Pattern)

`workflows/runner.py` → `WorkflowRunner`

Implements the Saga pattern from architecture_v3.md §6.

**Typed contracts** (`contracts/models.py`):
- `WorkflowDefinition`, `WorkflowStep`, `WorkflowStepResult`, `WorkflowResult`
- All use `extra="forbid"`

**Result statuses:**

| Status | Meaning |
|---|---|
| `COMPLETE` | All steps succeeded |
| `FAILED` | A step failed; no compensation recipes declared |
| `COMPENSATED` | A step failed; compensation recipes invoked for completed steps |
| `PARTIAL` | Reserved for future partial-completion modes |

`input_mapping` carries state forward between steps. Compensation runs in LIFO order through completed steps.

---

## 7. Hardening Controls

### Kill Switch

| Item | Detail |
|---|---|
| Env var | `ASOE_KILL_SWITCH` (`1` / `true` / `yes`, case-insensitive) |
| Files | `hardening/kill_switch.py` — `is_kill_switch_active()`, `apply_kill_switch()` |
| Behaviour | `run_graph()` returns immediately — zero nodes execute. `final_status` = `FAIL_TO_HUMAN`. TraceRecord still emitted. |
| Deactivation | `unset ASOE_KILL_SWITCH` or set to `0` / `false` / `no` |

### Explain Mode

| Item | Detail |
|---|---|
| Env var | `ASOE_EXPLAIN_MODE` (`1` / `true` / `yes`, case-insensitive) |
| Files | `hardening/explain_mode.py` — `is_explain_mode_active()`, `build_explain_summary()` |
| Graph | `orchestration/graph.py` → `build_explain_graph()` |
| Node | `orchestration/nodes.py` → `explain_only()` |
| Behaviour | Full reasoning pipeline runs (classify → shadow → select recipe → validate types). Stops before `execute_recipe`. Returns dry-run summary with `MANUAL_REVIEW_REQUIRED`. |

No process restart required for either switch — checked at each `run_graph()` call.

---

## 8. Policy Module

`contracts/policy.py` — single importable module for all business thresholds.

| Constant | Value | Consumed by |
|---|---|---|
| `MAX_DISCOUNT_ALLOWED` | `0.15` (15%) | `orchestration/nodes.py` → injected into `PriceAdjustmentRecipe` via `erp_context` |
| `PRICE_CONDITION_TYPE` | `"YK07"` (default; **per-tenant override required**) | `orchestration/nodes.py` → injected into `PriceAdjustmentRecipe` via `erp_context`. Condition types vary by SAP client configuration (e.g., `YK07`, `ZK07`, `PR00`). Must be overridable per tenant via the `policy_overrides` table. The `validate_types` node resolves the tenant-specific value before injection. |
| `CREDIT_AUTHORIZED_ROLES` | `("ORDER_MANAGER", "FINANCE_DIRECTOR")` | `orchestration/nodes.py` → injected into `CreditHoldReleaseRecipe` as param |
| `CREDIT_EXPOSURE_TOLERANCE` | `5_000.0` | `orchestration/nodes.py` → injected into `CreditHoldReleaseRecipe` as param |
| `DUPLICATE_PO_THRESHOLD_AUTO_BLOCK` | `0.90` | `orchestration/nodes.py` → injected into `DuplicatePORecipe` as param |
| `DUPLICATE_PO_THRESHOLD_REVIEW_REQUIRED` | `0.70` | `orchestration/nodes.py` → injected into `DuplicatePORecipe` as param |
| `DUPLICATE_PO_THRESHOLD_SOFT_FLAG` | `0.50` | `orchestration/nodes.py` → injected into `DuplicatePORecipe` as param |
| `DUPLICATE_PO_AUTONOMY_LEVELS` | `dict` (action → L1–L4) | `orchestration/nodes.py` → injected into `DuplicatePORecipe` as param |
| `MASS_UPDATE_LINE_COUNT_THRESHOLD` | `10` | `constraints/fallback_backend.py` |
| `CIRCUIT_BREAKER_MAX_UPDATES` | `50` | `orchestration/utils.py` |
| `CIRCUIT_BREAKER_MAX_VARIANCE` | `10_000.0` | `orchestration/utils.py`, `constraints/fallback_backend.py` |
| `DISCREPANCY_THRESHOLD` | `0.15` | `orchestration/utils.py` |

**Design principle:** Recipes never import from `policy.py`. All thresholds are injected by the orchestration layer (`validate_types` node) so the same recipe logic can serve different customer / vendor threshold sets.

Evolution path: module constants → env vars → K8s ConfigMap → per-customer policy service. The `policy_overrides` table (see architecture_v3.md §9.2) supports per-tenant overrides using dot-delimited hierarchical keys (e.g., `global.MAX_DISCOUNT_ALLOWED`, `tenant.acme.MAX_DISCOUNT_ALLOWED`). The `validate_types` node resolves from `policy_overrides` first, falling back to `contracts/policy.py` constants when no override exists.

### Duplicate PO Similarity Algorithm

The `signal_scores` composite score consumed by the `DUPLICATE_PO_THRESHOLD_*` thresholds is a weighted average of three deterministic signals:

| Signal | Weight | Method | Description |
|---|---|---|---|
| `po_number_similarity` | 0.40 | Normalized Levenshtein distance | Character-level similarity between candidate and matched PO numbers |
| `line_item_overlap` | 0.35 | Jaccard index on `(SKU, quantity)` tuples | Shared line items between the two POs |
| `temporal_proximity` | 0.25 | Exponential decay over hours since `matched_po.created_at` | POs submitted within minutes of each other score higher |

Composite score `= 0.40 × po_number_similarity + 0.35 × line_item_overlap + 0.25 × temporal_proximity`. All signals are computed deterministically by `DuplicatePORecipe` — no embedding or ML model. Weights are V1 constants; per-tenant overrides via `policy_overrides` are the evolution path.

---

## 9. Observability

`observability/tracer.py` → `TraceRecord` (Pydantic model, LangFuse-aligned)

Emitted via stdlib logging to the `asoe.observability` logger. Fields:

| Field | Description |
|---|---|
| `trace_id` | UUID propagated from `ComplianceDecision` → `ExecutionLog` |
| `event_id` | `OrderEvent.order_id` |
| `skill_name` | Name of the loaded `SkillDocument` |
| `intent_selected` | Constrained intent value |
| `shadow_verdict` | `GREEN` / `YELLOW` / `RED` |
| `shadow_policy_hits` | List of policy identifiers that fired |
| `recipe_name` | Selected recipe filename (or `null`) |
| `rag_chunks` | Reserved for V2 RAG integration — always empty in V1.0 (forward-compatible field) |
| `constrained_output_schemas` | Map of layer → schema name (e.g. `intent → IntentDecision`) |
| `gateway_calls` | Gateway operations invoked (dependency resolutions + effect applications) |
| `backend_fallback` | Which backend tier served this request: `"custom"`, `"outlines"`, or `"deterministic_fallback"` |
| `is_fallback_generated` | `true` if `backend_fallback == "deterministic_fallback"` — excluded from V2 fine-tuning datasets (see architecture_v3.md §12) |
| `final_status` | `COMPLETE`, `COMPLETE_WITH_CHILDREN`, `FAIL_TO_HUMAN`, `BLOCKED`, `MANUAL_REVIEW_REQUIRED`, `REJECTED` |
| `explanation` | Human-readable reason for the terminal decision |
| `llm_calls` | List of `LLMCallTrace` — one entry per remote-LLM trio call. Empty when the run was served by the deterministic backend. |
| `llm_total_input_tokens` | Sum across `llm_calls` (uncached portion) |
| `llm_total_output_tokens` | Sum across `llm_calls` |
| `llm_total_cache_read_tokens` | Sum across `llm_calls` |
| `llm_total_cache_creation_tokens` | Sum across `llm_calls` |
| `llm_total_cost_usd_estimate` | Sum across `llm_calls` (via `LLM_PRICING_USD_PER_M_TOKENS`) |
| `llm_any_fallback` | True if any LLM call fell through to deterministic (provider error / circuit open / budget block / validation failure) |
| `llm_cross_check_disagreement` | True if intent classify cross-check disagreed and the run was routed to `MANUAL_REVIEW_REQUIRED` |

**`LLMCallTrace`** (`contracts/models.py`) per-call schema:

| Field | Description |
|---|---|
| `task` | `"intent"` / `"recipe"` / `"shadow"` |
| `provider` | `LLMProvider` value used (`"anthropic"` / `"openai"` / etc.) |
| `model_id` | Resolved model id from the provider response |
| `request_id` | Provider request id (Anthropic `request-id`, OpenAI `x-request-id`, HF completion id). Audit-bearing per `LLMProvenance`. |
| `prompt_hash` | SHA-256 of system + tools bytes (cache stability check). Never holds prompt content. |
| `tool_call_hash` | SHA-256 of tool name + canonicalised arguments |
| `input_tokens` | Uncached prompt tokens (charged at full input rate) |
| `output_tokens` | Completion tokens |
| `cache_read_input_tokens` | Tokens served from prompt cache (~0.1× input) |
| `cache_creation_input_tokens` | Tokens written to cache (~1.25× input, 5-min TTL on Anthropic) |
| `latency_ms` | Wall-clock provider latency |
| `cost_usd_estimate` | USD spend from policy pricing table |
| `stop_reason` | Provider-native stop reason |
| `skill_md_version` | SHA-256 of the SKILL.md catalog at call time |
| `fallback_to_deterministic` | True when remote failed and deterministic served the call |
| `fallback_reason` | `'rate_limit'` / `'timeout'` / `'circuit_open'` / `'budget_hard_block'` / `'validation_error'` / etc. |
| `cross_check_disagreement` | (intent only) True when LLM and deterministic produced different intents |
| `cross_check_llm_intent` | (intent only) The LLM's intent at the disagreement point |
| `cross_check_deterministic_intent` | (intent only) The deterministic intent |

JSON-serialisable. Field-compatible with LangFuse trace schema.

### LangFuse Forwarding (Optional)

`observability/langfuse_sink.py` → `forward()`, `flush()`, `reset_client()`

When `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set and the `langfuse` package is installed, `Tracer.emit()` forwards each `TraceRecord` to LangFuse in addition to stdlib logging.

| LangFuse entity | ASOE source |
|---|---|
| `trace.id` | `TraceRecord.trace_id` |
| `trace.name` | `"asoe-graph-execution"` |
| `trace.input` | `{ event_id }` |
| `trace.output` | `{ final_status, explanation }` |
| `trace.metadata` | `{ constrained_output_schemas, gateway_calls, rag_chunks, llm_total_input_tokens, llm_total_output_tokens, llm_total_cost_usd_estimate, llm_any_fallback, llm_cross_check_disagreement }` |
| span `ingest` | `event_id` (always when graph ran) |
| span `classify` | `intent_selected`; `metadata.backend_used = "<provider>:<model_id>"` or `"deterministic"`; `metadata.constrained_by = constrained_output_schemas["intent"]`; `level=WARNING` on cross-check disagreement |
| span `load_skill` | `skill_name` |
| span `validate_circuit_breaker` | always when graph ran; `output.breached = True` and `level=WARNING` when `final_status=FAIL_TO_HUMAN` with no recipe / no shadow signal |
| span `select_recipe` | `recipe_name`; `metadata.backend_used`; `metadata.constrained_by = constrained_output_schemas["recipe"]` |
| span `resolve_dependencies` | `gateway_calls` entries with `dep:` prefix |
| span `validate_types` | `recipe_name` |
| span `shadow_audit` | `shadow_verdict`, `shadow_policy_hits`; `metadata.backend_used`; `metadata.constrained_by = constrained_output_schemas["shadow"]`; `level=WARNING` if non-GREEN |
| span `execute_recipe` | `recipe_name` (only when `shadow_verdict=GREEN` — YELLOW/RED halt at shadow); `metadata` carries `resolved_by` / `resolution_notes` for human overrides |
| span `apply_effects` | `gateway_calls` entries without `dep:` prefix (effect WRITES) |
| span `build_analysis` | `final_status`, `explanation` (Pillar 2 composer; always at terminal) |
| **generation** `llm.intent` / `llm.recipe` / `llm.shadow` | One per `LLMCallTrace`. Attached as a **child of the owning step span** (intent → classify, recipe → select_recipe, shadow → shadow_audit) so the LangFuse UI surfaces the call inline with the step. Native LangFuse generation observation: `model` = resolved model_id, `usage` = `{input, output, total, unit:"TOKENS"}`, `metadata` = provider, request_id, prompt_hash, cache hits, cost, fallback flags, cross-check signals. `level=WARNING` on `fallback_to_deterministic` OR `cross_check_disagreement`. Orphan generations (LLM ran but the owning step span gated out) attach to the trace root rather than being dropped. **Prompt content NEVER forwarded** — only hashes (Chen review §6 PII guard). |
| score `terminal_status` | 1.0 if COMPLETE, 0.0 otherwise |

**`terminal_status` score values:**

| `final_status` | Score `value` | Meaning |
|---|---|---|
| `COMPLETE` | **1.0** | Recipe executed successfully |
| `COMPLETE_WITH_CHILDREN` | **1.0** | Recipe executed; child exceptions spawned for secondary intents |
| `FAIL_TO_HUMAN` | 0.0 | Escalated to human (circuit breaker, missing params, gateway failure) |
| `MANUAL_REVIEW_REQUIRED` | 0.0 | Shadow returned YELLOW — requires review |
| `BLOCKED` | 0.0 | Shadow returned RED — halted by policy |
| `REJECTED` | 0.0 | Rejected by policy |

**SDK compatibility:** The sink auto-detects the installed langfuse SDK version via `_is_v2()`. Langfuse v2 (`client.trace()` → `trace.span()` / `trace.score()`) and v4+ (`client.start_observation()` / `client.create_score()`) are both supported. See `prompts/phase_10_langfuse.md` for the full integration specification.

**Failure isolation:** All LangFuse errors are caught and logged at WARNING/DEBUG level. Forwarding failures never block graph execution. Stdlib logging remains the authoritative audit record.

---

## 10. Circuit Breaker

`orchestration/nodes.py` → `validate_circuit_breaker()` node
`orchestration/utils.py` → `circuit_breaker()` function

| Threshold | Limit | Source | Action |
|---|---|---|---|
| Update count | > 50 per batch | `policy.CIRCUIT_BREAKER_MAX_UPDATES` | `FAIL_TO_HUMAN` |
| Total dollar variance | > $10,000 per batch | `policy.CIRCUIT_BREAKER_MAX_VARIANCE` | `FAIL_TO_HUMAN` |

Evaluated on every graph run, **before** shadow audit and recipe selection.

---

## 11. Secret Management (Kubernetes)

| Manifest | Purpose |
|---|---|
| `k8s/core/secret-provider.yaml` | `SecretProviderClass` — syncs Azure Key Vault secrets to Kubernetes Secret (`asoe-secrets`) |
| `k8s/core/deployment.yaml` | Mounts secrets-store volume, references `asoe-secrets` via `envFrom.secretRef` |

Pods authenticate via Azure Workload Identity (temporary tokens). No credentials in source code, Dockerfiles, or env var defaults.

---

## 12. Environment Variable Reference

| Variable | Default | Description |
|---|---|---|
| `ASOE_KILL_SWITCH` | `0` | `1` / `true` / `yes` → halt all execution |
| `ASOE_EXPLAIN_MODE` | `0` | `1` / `true` / `yes` → dry-run only, no recipe execution |
| `USE_OUTLINES_BACKEND` | `0` | `1` → use `OutlinesConstrainedBackend` (requires `outlines` package) |
| `LANGFUSE_PUBLIC_KEY` | _(unset)_ | LangFuse public key — enables trace forwarding when set (requires `langfuse` package) |
| `LANGFUSE_SECRET_KEY` | _(unset)_ | LangFuse secret key — required alongside public key |
| `LANGFUSE_HOST` | _(unset)_ | LangFuse host URL — omit for LangFuse Cloud, set for self-hosted |
| `ASOE_LLM_PROVIDER` | `fallback` | Global default for the constrained-generation trio. Allowed values: `anthropic` / `openai` / `google` / `ollama` / `huggingface` / `outlines` / `local` / `fallback`. |
| `ASOE_LLM_PROVIDER_INTENT` | _(unset)_ | Per-task override for intent classification |
| `ASOE_LLM_PROVIDER_RECIPE` | _(unset)_ | Per-task override for recipe selection |
| `ASOE_LLM_PROVIDER_SHADOW` | _(unset)_ | Per-task override for shadow audit (defaults to deterministic in V1 PR-1) |
| `ASOE_LLM_DISABLE_FOR` | _(unset)_ | Comma-list of trio tasks pinned to deterministic regardless of provider config (`intent,recipe,shadow`). Runtime kill-by-task. |
| `ASOE_LLM_DAILY_USD_BUDGET` | `5.00` | Daily USD spend cap. At 100% the LLM tier hard-blocks to deterministic for the rest of the UTC day. Re-checked per call. |
| `ANTHROPIC_API_KEY` | _(unset)_ | Required when `ASOE_LLM_PROVIDER=anthropic`. Production must come from Azure Key Vault CSI. |
| `ANTHROPIC_BASE_URL` | _(unset)_ | Override the SDK default. Set to Azure AI Foundry private endpoint URL in production. Sandbox can leave unset to use api.anthropic.com. |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Anthropic model alias |
| `ANTHROPIC_DEPLOYMENT` | _(unset)_ | Foundry deployment name (forwarded as `x-azure-deployment` header) |
| `ANTHROPIC_API_VERSION` | _(unset)_ | API version override (anthropic-version header) |
| `OPENAI_API_KEY` | _(unset)_ | Required when `ASOE_LLM_PROVIDER=openai`. Self-hosted vLLM/TGI/LiteLLM accepts any non-empty placeholder. |
| `OPENAI_BASE_URL` | _(unset)_ | OpenAI direct (unset → api.openai.com, sandbox-only by policy), Azure OpenAI resource endpoint, or self-hosted OpenAI-compatible cluster URL. |
| `OPENAI_API_VERSION` | _(unset)_ | Setting this auto-selects the `AzureOpenAI` SDK class. |
| `OPENAI_DEPLOYMENT` | _(unset)_ | Azure OpenAI deployment name (becomes call-time `model` arg). |
| `OPENAI_MODEL` | `claude-sonnet-4-6` (default) | Model id when not Azure (e.g. `gpt-4o`, `Qwen/Qwen2.5-32B-Instruct` for vLLM). |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama host URL. Production must be self-hosted or private-peered (public Ollama Cloud blocked). |
| `OLLAMA_API_KEY` | _(unset)_ | Optional bearer token for proxied auth setups. |
| `OLLAMA_MODEL` | _(unset)_ | Ollama model id (e.g. `qwen2.5`, `llama3.1:70b`). |
| `HUGGINGFACE_API_KEY` | _(unset)_ | Required when `ASOE_LLM_PROVIDER=huggingface`. |
| `HUGGINGFACE_BASE_URL` | _(unset)_ | Dedicated Inference Endpoint URL (production). Unset → Serverless Inference API (sandbox-only). |
| `HUGGINGFACE_MODEL` | _(unset)_ | HF model id (e.g. `Qwen/Qwen2.5-32B-Instruct`, `meta-llama/Llama-3.1-70B-Instruct`). |

Per-provider env vars also include `*_TIMEOUT_S`, `*_MAX_RETRIES`, `*_REGION`, `*_PROJECT_ID` (Google), `*_EXTRA_HEADERS` (semicolon-separated `key: value` pairs). See `llm/anthropic_client.py::RemoteLLMConfig.from_env` for the full pattern; every provider follows the same prefix convention.

---

## 13. Container Architecture

Three-container split (mirrors dependency groups in `pyproject.toml`):

| Container | Dockerfile | Contents |
|---|---|---|
| Core | `Dockerfile.core` | Orchestration engine, recipes, Compliance Shadow, LangFuse |
| UI | `Dockerfile.ui` | Streamlit sandbox UI (core + streamlit + LangFuse, no GPU deps) |
| Inference | `Dockerfile.inference` | Local LLM inference (Outlines + torch + transformers, no LangFuse) |

All images: non-root user (`asoe`, UID 1000), `uv` for deterministic dependency resolution.

Local dev: `docker-compose.yml` (core + ui always-on, inference via `--profile inference`).
Production: Kubernetes manifests in `k8s/` (namespace, deployments, services, secret provider).

---

## 14. Exception Lifecycle & HITL Protocol (Target State)

The following are defined in architecture_v3.md and will be implemented as the FastAPI layer is built:

- **11-state exception lifecycle** (INGESTED → CLOSED, including ESCALATED) — architecture_v3.md §9.1
- **HITL pause/resume** via LangGraph `interrupt()` + `PostgresSaver` checkpoints — architecture_v3.md §5.9
- **Two-tier timeout escalation** (48h default + 24h escalation window) — architecture_v3.md §5.9

> **Current V1 behavior:** The graph terminates on YELLOW verdict (`final_status = MANUAL_REVIEW_REQUIRED`). The full interrupt/checkpoint/resume mechanism is a V1.1 target.

> **Status surfaces:** for the full set of status/state fields — Intent, Shadow verdict, `final_status` (`TerminalStatus`), the 12-state `lifecycle_state`, disposition sub-type, and `OrderCase.status` — plus the deterministic derivation maps between them, see `docs/STATUS_MODEL.md`.

---

## 15. API Layer (FastAPI)

Implements architecture_v3.md §8 (API Contract), §11.1 (Authentication), §11.2 (RBAC), §11.3 (Multi-Tenancy).

### 15.1 REST Endpoints

| Method | Path | Auth | Handler |
|---|---|---|---|
| `GET` | `/api/v1/health` | public | `api/routes/health.py::health()` |
| `POST` | `/api/v1/exceptions/resolve` | analyst+ | `api/routes/exceptions.py::resolve()` |
| `POST` | `/api/v1/exceptions/resolve/async` | analyst+ | `api/routes/exceptions.py::resolve_async()` |
| `POST` | `/api/v1/exceptions/resolve/explain` | analyst+ | `api/routes/exceptions.py::resolve_explain()` |
| `GET` | `/api/v1/exceptions` | analyst+ | `api/routes/exceptions.py::list_exceptions()` |
| `GET` | `/api/v1/exceptions/stats` | analyst+ | `api/routes/exceptions.py::stats()` |
| `GET` | `/api/v1/exceptions/{id}` | analyst+ | `api/routes/exceptions.py::get_exception()` |
| `GET` | `/api/v1/exceptions/{id}/trace` | analyst+ | `api/routes/exceptions.py::get_trace()` |
| `PATCH` | `/api/v1/exceptions/{id}/override` | manager+ | `api/routes/exceptions.py::override_exception()` |
| `POST` | `/api/v1/exceptions/{id}/approve` | manager+ | `api/routes/exceptions.py::approve_exception()` |
| `POST` | `/api/v1/exceptions/{id}/reject` | manager+ | `api/routes/exceptions.py::reject_exception()` |
| `POST` | `/api/v1/workflows` | manager+ | `api/routes/workflows.py::run_workflow()` |
| `PUT` | `/api/v1/policies/{tenant_id}` | admin | `api/routes/policies.py::update_policy()` |
| `POST` | `/api/auth/login` | public | `api/routes/auth.py::login()` |
| `POST` | `/api/auth/sso/init` | public | `api/routes/auth.py::sso_init()` |
| `GET` | `/api/auth/sso/callback` | public | `api/routes/auth.py::sso_callback()` |
| `POST` | `/api/auth/mfa/verify` | public | `api/routes/auth.py::mfa_verify()` |
| `POST` | `/api/auth/refresh` | public | `api/routes/auth.py::refresh()` |
| `GET` | `/api/auth/me` | any | `api/routes/auth.py::me()` |
| `POST` | `/api/auth/switch` | any (sandbox only) | `api/routes/auth.py::switch_user()` |
| `GET` | `/api/auth/users` | any (sandbox only) | `api/routes/auth.py::list_available_users()` |
| `GET` | `/api/v1/accounts` | analyst+ | `api/routes/accounts.py::get_accounts()` |

### 15.2 Authentication & RBAC

**JWT validation** (`api/deps.py`): Extracts Bearer token from `Authorization` header. Validates signature (HS256), expiry (`exp` claim), and environment (`env` claim vs `ASOE_ENV`). Secret loaded from `ASOE_JWT_SECRET` env var (dev fallback when unset).

**Token types** (architecture_v3.md §11.1):

| Type | Lifetime | `token_type` claim | Created by |
|---|---|---|---|
| Access | 15 minutes | `access` | `create_access_token()` |
| Refresh | 7 days | `refresh` | `create_refresh_token()` |

Refresh endpoint validates `token_type == "refresh"` and issues rotated tokens. `auth_method` claim (`"sso"`, `"password+mfa"`) set for audit differentiation.

**Role → Permission mapping** (5 roles from architecture_v3.md §11.2):

| Role | Key Permissions |
|---|---|
| `analyst` | `exceptions:read`, `exceptions:approve` |
| `manager` | analyst + `exceptions:override`, `rules:write` |
| `admin` | manager + `users:manage`, `policy:write`, `audit:read` |
| `viewer` | `exceptions:read`, `dashboard:read` |
| `partner` | `exceptions:read` (scoped to own orders via `retailer_id`) |

**User store** (`api/users.py`): Server-side user profiles (6 seed users) and Account entity (4 accounts: Walmart, Kroger, Target, Costco). Login resolves against the user store instead of hardcoded credentials. `compute_visible_tabs()` derives tab visibility from the user's expanded permissions. `expand_permissions()` mirrors `deps._expand_permissions()`.

**Additional JWT claims**: `title`, `avatar_initials`, and `assigned_accounts` are included in access and refresh tokens. `AuthenticatedUser` (deps.py) carries these fields. `assigned_accounts` scopes the user to specific retail customer accounts (empty list = all accounts).

**Account scoping**: Users with `assigned_accounts` see only their assigned accounts in `GET /api/v1/accounts` and only exceptions matching those account IDs in exception list/detail endpoints. The `ExceptionRecord` and `ExceptionSummary`/`ExceptionDetail` schemas include `account_id` and `account_name` fields.

**Sandbox-only endpoints**: `POST /api/auth/switch` issues a new JWT for a different user (blocked in production via `ASOE_ENV` check). `GET /api/auth/users` lists all available users for the sandbox user switcher (also blocked in production).

**Tenant isolation** (`api/deps.py::get_tenant_id()`): Extracts `tenant_id` from JWT `org` claim. All queries scoped by `tenant_id`. Partner-role filtering by `retailer_id` claim (§11.3). PostgreSQL RLS provides defense-in-depth.

**Environment isolation** (§11.6): JWT `env` claim validated against `ASOE_ENV` env var. Mismatch → 403 with generic "Access denied." (no internal state leaked).

### 15.3 Middleware

**X-Trace-ID** (`api/middleware.py::TraceIDMiddleware`): Propagates client `X-Trace-ID` header or generates UUID at the API boundary. Stored in `request.state.trace_id` and returned in every response header. Implements §11.4.

### 15.3 Error Envelope

All errors use the standard envelope from architecture_v3.md §8.3:

```json
{
  "error": {
    "code": "SHADOW_BLOCKED",
    "message": "...",
    "trace_id": "...",
    "details": { ... }
  }
}
```

### 15.4 Persistence

Two backends available, selected by `DATABASE_URL` env var:

| Backend | When | Store Class |
|---|---|---|
| In-memory | `DATABASE_URL` unset (default) | `ExceptionStore` |
| Database | `DATABASE_URL` set (SQLite or PostgreSQL) | `DatabaseBackedStore` |

Both backends expose the same interface — API routes work unchanged regardless of backend.

---

## 16. Database Layer

Implements architecture_v3.md §9.2 (PostgreSQL schema), §9.1 (lifecycle states), §11.3 (tenant isolation).

### 16.1 Schema (V001)

5 tables defined in `db/migrations/V001__initial_schema.sql`:

| Table | Purpose | Key Fields |
|---|---|---|
| `exceptions` | Exception state + audit trail | `id`, `tenant_id`, `order_id`, `intent`, `lifecycle_state`, `trace_id`, `resolution_data` (JSONB) |
| `traces` | Full TraceRecord per execution | `exception_id` (FK), `trace_record` (JSONB) |
| `policy_overrides` | Per-tenant policy values | `tenant_id`, `policy_key`, `value` (JSONB), `effective_from` |
| `policy_audit_log` | Immutable SOX audit trail | `previous_value`, `new_value`, `changed_by` — immutability enforced by trigger |
| `checkpoints` | HITL pause/resume state (V1.1) | `trace_id` (PK), `graph_state` (JSONB), `status` |

### 16.2 Row-Level Security (PostgreSQL only)

RLS policies on `exceptions`, `traces`, `policy_overrides`, `checkpoints` enforce tenant isolation at the database layer. The connection adapter sets `app.current_tenant_id` session variable; RLS policy returns zero rows when the variable is unset (defense-in-depth).

### 16.3 Connection Adapters

| Adapter | Backend | Usage |
|---|---|---|
| `SQLiteAdapter` | SQLite (stdlib) | Testing, local dev |
| `PostgresAdapter` | PostgreSQL (psycopg2/psycopg) | Production |

Factory: `create_adapter(database_url)` auto-detects from URL scheme.

### 16.4 Repository Layer

| Repository | Table | Operations |
|---|---|---|
| `ExceptionRepository` | `exceptions` | create, get, list (paginated), update, stats |
| `TraceRepository` | `traces` | create, get_by_exception |
| `PolicyRepository` | `policy_overrides` + `policy_audit_log` | create_override (with audit), get_override, list_audit_log |

All queries include `tenant_id` predicate for application-layer isolation.

### 16.5 Remaining Design References

| Concern | Architecture Reference | Status |
|---|---|---|
| HITL pause/resume (interrupt + checkpoint) | architecture_v3.md §5.9 | Planned (V1.1) |
| Deployment model rationale | [ADR-001](docs/adr/ADR-001-core-deployment-model.md) | — |
| Database access pattern rationale | [ADR-002](docs/adr/ADR-002-database-access-pattern.md) | — |

---

## 17. Real-Time Event Publishing (WebSocket / Redis)

Implements architecture_v3.md §10 (Real-Time Event Publishing) and §9.3 (Redis Usage).

### 17.1 Event Schemas (`api/events.py`)

| Event Type | Payload Model | Published When |
|---|---|---|
| `pipeline_progress` | `PipelineProgressPayload` (node, status, duration_ms, data) | Each LangGraph node completes |
| `exception_update` | `ExceptionUpdatePayload` (lifecycle_state, updated_fields) | Lifecycle state transitions |
| `task_complete` | `TaskCompletePayload` (task_id, final_status, explanation) | Graph execution finishes |
| `error` | `ErrorPayload` (code, message) | Pipeline errors |

All events share the `WSEvent` envelope: `type`, `trace_id`, `exception_id`, `tenant_id`, `timestamp`, `payload`.

### 17.2 Pub/Sub Backends (`api/pubsub.py`)

| Backend | When | Implementation |
|---|---|---|
| `InMemoryPubSub` | `REDIS_URL` unset (testing/dev) | Thread-safe per-tenant lists with timestamp-based replay |
| `RedisPubSub` | `REDIS_URL` set (production) | Redis Pub/Sub channels + sorted-set replay buffer (60s TTL) |

Factory: `create_pubsub()` auto-detects from `REDIS_URL`. Module-level `event_publisher` singleton.

Channels: `asoe:ws:{tenant_id}`. Replay buffer: `asoe:replay:{tenant_id}` (sorted set, score = timestamp).

Publish failures are logged at WARNING and never block API responses (§9.3 partial failure recovery).

### 17.3 WebSocket Hub (`api/routes/ws.py`)

`ws://host/api/v1/ws` — authenticated, tenant-scoped event streaming.

**Protocol:**
1. Client sends `{ "type": "auth", "token": "eyJ..." }` — server validates JWT, extracts `tenant_id`
2. Optional `last_seen` ISO 8601 timestamp triggers replay from the 60s buffer
3. In-memory mode: client sends `{ "type": "ping" }`, server returns new events + `{ "type": "pong" }`
4. Redis mode: server subscribes to `asoe:ws:{tenant_id}` channel and forwards events

**Tenant isolation:** Each client receives events only for their `tenant_id` channel.

### 17.4 Resolve Endpoint Integration

All three resolve endpoints (sync, async, explain) publish a `task_complete` event after graph execution via `_publish_task_complete()`. Events include `trace_id`, `exception_id`, `tenant_id`, `final_status`, and `explanation`.

---

## 18. Local Execution Sandbox

| Component | File | Purpose |
|---|---|---|
| Seeder | `tests/sandbox/seed.py` | Seeds SQLite with customers, DCs, promotions, SAP pricing, retailer contracts, credit profiles, 18 EDI events |
| UI | `tests/sandbox/ui/app.py` | Streamlit — select event, run full graph, inspect execution trace |
| Local LLM | `tests/sandbox/llm/local_backend.py` | `LocalHFBackend` — Outlines constrained-JSON via local HuggingFace model; falls back to `DeterministicFallbackBackend` |
| Prompts | `tests/sandbox/llm/prompts.py` | `intent_prompt()`, `recipe_prompt()`, `shadow_prompt()` — prompt transparency for demos |
| Dependencies | `tests/sandbox/requirements-sandbox.txt` | Isolated from CI/production requirements |

---

## 19. Test Coverage

| File | Coverage area |
|---|---|
| `test_api.py` | FastAPI endpoints, JWT auth, RBAC, tenant isolation, error envelope |
| `test_security.py` | Token expiry, access/refresh types, env isolation, trace_id, partner scoping, secret config |
| `test_websocket.py` | Event schemas, InMemoryPubSub, resolve event publishing, WebSocket auth + streaming + tenant isolation |
| `test_v1_guardrails.py` | 6 V1 Foundation Guardrails (AST inspection, dynamic enums, metadata contracts, ERP-agnostic gateway, schema agnostic, policy key format) + Invariant #11 |
| `test_db.py` | Database schema, repositories, tenant isolation, pagination, audit log |
| `test_constraints.py` | Constrained output schemas and backend chain |
| `test_contracts.py` | Pydantic model validation |
| `test_executor.py` | RecipeExecutor dispatch and error handling |
| `test_gateways.py` | Gateway protocol, registry, executor, stub |
| `test_golden.py` | Full pipeline regression (all intents end-to-end) |
| `test_graph_paths.py` | LangGraph state machine paths |
| `test_hardening.py` | Kill switch and explain mode |
| `test_intent_classifier.py` | Intent classification logic |
| `test_nodes.py` | Individual graph node functions |
| `test_observability.py` | TraceRecord emission and fields, LangFuse v2/v4 mapping incl. per-LLM-call generation spans |
| `test_recipes.py` | Recipe business logic |
| `test_registry.py` | Recipe registry operations |
| `test_router.py` | Constraint router — per-task routing, ASOE_LLM_PROVIDER, ASOE_LLM_DISABLE_FOR, kill-switch + explain-mode pinning |
| `test_anthropic_client.py` | AnthropicProviderClient (direct + Foundry) — config, prod-egress gate, kill-switch, call_with_tool, exception classification |
| `test_openai_client.py` | OpenAIProviderClient (OpenAI / Azure OpenAI / vLLM-compat) — Azure detection, prod-egress, prompt caching, exception matrix |
| `test_ollama_client.py` | OllamaProviderClient — self-hosted + Cloud, dict + JSON-string args, exception classification |
| `test_huggingface_client.py` | HuggingFaceProviderClient — Dedicated Endpoint vs Serverless detection, OpenAI-compatible response shape, exception matrix |
| `test_provider_stubs.py` | Stub providers (google) Protocol satisfaction + prod-egress gates |
| `test_provider_factory.py` | `build_provider_client(provider)` dispatch + UnknownProvider |
| `test_llm_backend.py` | RemoteLLMBackend trio coverage — happy path, fallback on ProviderError / Pydantic error / circuit / budget, deterministic prompt prefix, custom fallback injection |
| `test_llm_sanitizer.py` | OrderEvent.metadata allowlist / length cap / control-char scrub / untrusted-data delimiter |
| `test_llm_budget.py` | InMemoryBudgetTracker + RedisBudgetTracker, soft-warn / hard-block thresholds, factory dispatch |
| `test_llm_circuit_breaker.py` | LLM-tier breaker — sliding window trip / cooldown → HALF_OPEN / probe success closes / probe failure re-opens |
| `test_cross_check.py` | LLM/deterministic intent cross-check — agreement, disagreement reason, immutability |
| `test_llm_cache_invalidators.py` | Byte-identical system+tools prefix audit (cache-hit-rate regression guard) |
| `test_orchestration_llm_integration.py` | End-to-end per-task routing through orchestration nodes; cross-check disagreement → MANUAL_REVIEW_REQUIRED; kill-switch + explain-mode pinning verified through `_backend(task)` |
| `test_llm_provenance.py` | RemoteLLMBackend.last_call_trace populated on every exit branch; orchestration drains onto `state.llm_call_traces`; Tracer aggregates token totals + cost + flags |
| `test_llm_provenance_registry.py` | LLMProvenance section in `compliance/audit_bearing_registry.yaml` — audit-bearing classifications + `pending_signoff: true` markers |
| `test_anthropic_client.py`, `test_llm_policy.py` | Policy constants + RemoteLLMConfig env-loading |
| `test_shadow.py` | Compliance Shadow audit and enforcement |
| `test_skill_loader.py` | SKILL.md loading |
| `test_user_profiles.py` | User store, Account entity, JWT claims (title, avatar_initials, assigned_accounts), account scoping, sandbox user switching, visible_tabs |
| `test_workflows.py` | WorkflowRunner Saga execution and compensation |

```bash
python -m pytest   # All tests must pass
```
