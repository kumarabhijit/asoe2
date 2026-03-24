# Design Reference — CPG Agentic Pricing Exception System

**Implements:** [architecture_v2.md](architecture_v2.md) (patterns, principles, technology choices)
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
  PriceAdjustmentRecipe.py   # CONTRACTUAL_CORRECTION / MASS_PRICING_ERROR
  CreditHoldReleaseRecipe.py # CREDIT_BLOCK
  DuplicatePORecipe.py       # DUPLICATE_PO
  registry.py        # Recipe registry (name → spec mapping)
  executor.py        # RecipeExecutor — dispatches to registered recipes

compliance/
  shadow.py          # ComplianceShadow: audit() → ComplianceDecision, enforce() → ShadowEnforcement

constraints/
  specs.py           # Constrained output schemas: IntentDecision, ShadowDecision, RecipeProposal
  router.py          # ConstraintRouter — three-tier backend selection
  fallback_backend.py    # DeterministicFallbackBackend (no LLM, CI/test)
  outlines_backend.py    # OutlinesConstrainedBackend (production Outlines regex)
  guidance_backend.py    # GuidanceRegexBackend (regex patterns for Guidance / Outlines)

llm/
  backends.py        # get_outlines_model() — cached Outlines + HuggingFace model loader (used by OutlinesConstrainedBackend)

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

tests/
  conftest.py        # Shared fixtures (StubGateway, sample events, backend setup)
  test_*.py          # Unit and integration tests (16 files, 525 tests)
  sandbox/           # Interactive exploration tools (see §10)
```

---

## 2. Constraint Backend Chain

The `ConstraintRouter` in `constraints/router.py` selects the active backend using a three-tier fallback:

```
Custom backend (env var)  →  OutlinesConstrainedBackend  →  DeterministicFallbackBackend
```

Each backend implements three methods:
- `classify_intent(event_data)` → `IntentDecision`
- `propose_recipe(intent, event_data)` → `RecipeProposal`
- `shadow_decision(proposal)` → `ShadowDecision`

If `OutlinesConstrainedBackend` fails to initialise (missing `outlines` package), the router degrades to `DeterministicFallbackBackend` with a `logger.warning()`.

**Constrained output types** (`constraints/specs.py`):

| Schema | Constrained field | Allowed values |
|---|---|---|
| `IntentDecision` | `AllowedIntent` | `CONTRACTUAL_CORRECTION`, `CREDIT_BLOCK`, `MASS_PRICING_ERROR`, `DUPLICATE_PO` |
| `ShadowDecision` | `AllowedShadowStatus` | `GREEN`, `YELLOW`, `RED` |
| `RecipeProposal` | `AllowedRecipeName` | `PriceAdjustmentRecipe.py`, `CreditHoldReleaseRecipe.py`, `DuplicatePORecipe.py` |

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

Implements the Hexagonal Gateway pattern from architecture_v2.md §4G.

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

Implements the Saga pattern from architecture_v2.md §3.

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
| `PRICE_CONDITION_TYPE` | `"YK07"` | `orchestration/nodes.py` → injected into `PriceAdjustmentRecipe` via `erp_context` |
| `CREDIT_AUTHORIZED_ROLES` | `("ORDER_MANAGER", "FINANCE_DIRECTOR")` | `orchestration/nodes.py` → injected into `CreditHoldReleaseRecipe` as param |
| `CREDIT_EXPOSURE_TOLERANCE` | `5_000.0` | `orchestration/nodes.py` → injected into `CreditHoldReleaseRecipe` as param |
| `DUPLICATE_PO_THRESHOLD_AUTO_BLOCK` | `0.90` | `orchestration/nodes.py` → injected into `DuplicatePORecipe` as param |
| `DUPLICATE_PO_THRESHOLD_REVIEW_REQUIRED` | `0.70` | `orchestration/nodes.py` → injected into `DuplicatePORecipe` as param |
| `DUPLICATE_PO_THRESHOLD_SOFT_FLAG` | `0.50` | `orchestration/nodes.py` → injected into `DuplicatePORecipe` as param |
| `MASS_UPDATE_LINE_COUNT_THRESHOLD` | `10` | `constraints/fallback_backend.py` |
| `CIRCUIT_BREAKER_MAX_UPDATES` | `50` | `orchestration/utils.py` |
| `CIRCUIT_BREAKER_MAX_VARIANCE` | `10_000.0` | `orchestration/utils.py`, `constraints/fallback_backend.py` |
| `DISCREPANCY_THRESHOLD` | `0.15` | `orchestration/utils.py` |

**Design principle:** Recipes never import from `policy.py`. All thresholds are injected by the orchestration layer (`validate_types` node) so the same recipe logic can serve different customer / vendor threshold sets.

Evolution path: module constants → env vars → K8s ConfigMap → per-customer policy service.

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
| `final_status` | `COMPLETE`, `FAIL_TO_HUMAN`, `BLOCKED`, `MANUAL_REVIEW_REQUIRED`, `REJECTED` |
| `explanation` | Human-readable reason for the terminal decision |

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
| `trace.metadata` | `{ constrained_output_schemas, gateway_calls, rag_chunks }` |
| span `classify` | `intent_selected` |
| span `load_skill` | `skill_name` |
| span `shadow_audit` | `shadow_verdict`, `shadow_policy_hits` (level=WARNING if non-GREEN) |
| span `execute_recipe` | `recipe_name` |
| score `terminal_status` | 1.0 if COMPLETE, 0.0 otherwise |

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

## 14. Local Execution Sandbox

| Component | File | Purpose |
|---|---|---|
| Seeder | `tests/sandbox/seed.py` | Seeds SQLite with customers, DCs, promotions, SAP pricing, retailer contracts, credit profiles, 18 EDI events |
| UI | `tests/sandbox/ui/app.py` | Streamlit — select event, run full graph, inspect execution trace |
| Local LLM | `tests/sandbox/llm/local_backend.py` | `LocalHFBackend` — Outlines constrained-JSON via local HuggingFace model; falls back to `DeterministicFallbackBackend` |
| Prompts | `tests/sandbox/llm/prompts.py` | `intent_prompt()`, `recipe_prompt()`, `shadow_prompt()` — prompt transparency for demos |
| Dependencies | `tests/sandbox/requirements-sandbox.txt` | Isolated from CI/production requirements |

---

## 15. Test Coverage

| File | Coverage area |
|---|---|
| `test_constraints.py` | Constrained output schemas and backend chain |
| `test_contracts.py` | Pydantic model validation |
| `test_executor.py` | RecipeExecutor dispatch and error handling |
| `test_gateways.py` | Gateway protocol, registry, executor, stub |
| `test_golden.py` | Full pipeline regression (all intents end-to-end) |
| `test_graph_paths.py` | LangGraph state machine paths |
| `test_hardening.py` | Kill switch and explain mode |
| `test_intent_classifier.py` | Intent classification logic |
| `test_nodes.py` | Individual graph node functions |
| `test_observability.py` | TraceRecord emission and fields |
| `test_recipes.py` | Recipe business logic |
| `test_registry.py` | Recipe registry operations |
| `test_router.py` | Constraint router fallback chain |
| `test_shadow.py` | Compliance Shadow audit and enforcement |
| `test_skill_loader.py` | SKILL.md loading |
| `test_workflows.py` | WorkflowRunner Saga execution and compensation |

```bash
python -m pytest   # Expected: 525 passed
```
