# Auditor Guide — CPG Agentic Pricing Exception System

**Document owner:** Principal AI Systems Architect
**Scope:** Phase 6 hardening controls, Guidance / Outlines constrained-generation safeguards, and post-review remediation
**Status:** Production-ready (Post-Review, Grade A-)

---

## 1. System Overview

The CPG Agentic Pricing Exception System is a **deterministic, compliance-first**
orchestration layer for pricing and promotional exception handling.  It follows a
**Skill–Shadow–Recipe** architecture:

| Layer | Component | Role |
|---|---|---|
| Skill | `skills/SKILL.md` | Classifies intent; guides reasoning only |
| Shadow | `compliance/shadow.py` | Audits every proposed action before execution |
| Recipe | `recipes/*.py` | Immutable deterministic business logic |
| Orchestrator | `orchestration/graph.py` | LangGraph state machine; routes state; no business logic |

The system **never** executes a recipe unless:
1. Intent has been classified to a constrained vocabulary.
2. The Compliance Shadow has returned `GREEN`.
3. Recipe selection has been constrained to a registered name.
4. All recipe parameters have been type-validated.

---

## 2. Constrained Generation Safeguards (Guidance / Outlines)

All LLM-generated values that flow into code, state transitions, or downstream
systems are **constrained at generation time**.  Free-form text is allowed only
for human-facing explanations.

### 2.1 Intent Classification

| Property | Value |
|---|---|
| **Where** | `orchestration/nodes.py` → `classify()` → `backend.classify_intent()` |
| **Schema** | `constraints/specs.py` → `IntentDecision` |
| **Allowed values** | `CONTRACTUAL_CORRECTION`, `CREDIT_BLOCK`, `MASS_PRICING_ERROR`, `DUPLICATE_PO` |
| **Enforcement** | `AllowedIntent` Pydantic `Literal` type; Pydantic raises `ValidationError` on any other value |
| **Backend** | `DeterministicFallbackBackend` (no LLM in CI/test) or `OutlinesConstrainedBackend` (production Outlines regex) |
| **Fallback chain** | Custom backend → `OutlinesConstrainedBackend` → `DeterministicFallbackBackend` (graceful degradation with `logger.warning()` on each failure) |

Any value outside the allowed set causes a `ValidationError` before the state
machine advances.  The system routes to `FAIL_TO_HUMAN`.

### 2.2 Compliance Shadow Verdict

| Property | Value |
|---|---|
| **Where** | `compliance/shadow.py` → `ComplianceShadow.audit()` → `backend.shadow_decision()` |
| **Schema** | `constraints/specs.py` → `ShadowDecision` |
| **Allowed values** | `GREEN`, `YELLOW`, `RED` |
| **Enforcement** | `AllowedShadowStatus` Pydantic `Literal`; `ShadowStatus` enum in `contracts/models.py` |
| **Routing** | `GREEN` → proceed · `YELLOW` → `MANUAL_REVIEW_REQUIRED` · `RED` → `BLOCKED` |

The Compliance Shadow verdict **cannot be overridden** by orchestration code.
`ShadowEnforcement.action` is derived purely from the constrained verdict.

#### Structured Shadow Logging

Both `audit()` and `enforce()` emit structured log records to the `asoe.compliance`
logger so that shadow decisions **survive graph crashes**.  Each log entry includes:

| Field | Source |
|---|---|
| `trace_id` | `ComplianceDecision.trace_id` |
| `status` | `GREEN` / `YELLOW` / `RED` |
| `reasons` | List of human-readable reason strings |
| `policy_hits` | List of policy identifiers that fired |
| `constrained_by` | Schema name used to constrain the verdict |

Enforcement logs are emitted at `INFO` for `GREEN` and `WARNING` for `YELLOW` / `RED`.

### 2.3 Recipe Selection

| Property | Value |
|---|---|
| **Where** | `orchestration/nodes.py` → `select_recipe()` → `backend.propose_recipe()` |
| **Schema** | `constraints/specs.py` → `RecipeProposal` |
| **Allowed values** | `PriceAdjustmentRecipe.py`, `CreditHoldReleaseRecipe.py`, `DuplicatePORecipe.py` |
| **Enforcement** | `AllowedRecipeName` Pydantic `Literal`; recipe registry (`recipes/registry.py`) as defence-in-depth |

An unregistered recipe name causes a `ValidationError` at the `RecipeProposal`
boundary and a `KeyError` at the registry — two independent guards.

### 2.4 Recipe Invocation Parameters

| Property | Value |
|---|---|
| **Where** | `orchestration/nodes.py` → `validate_types()` → `RecipeExecutor.run()` |
| **Schema** | `contracts/models.py` → `RecipeInvocation`; recipe-specific required-param lists in `recipes/registry.py` |
| **Enforcement** | `RecipeExecutor` checks all required params before dispatch; missing / `None` params produce `ExecutionLog.errors` and the node routes to `FAIL_TO_HUMAN` |

### 2.5 Input Validation at System Boundary

The `ingest()` node in `orchestration/nodes.py` validates every incoming event
via `_validate_event()` before the state machine advances.  Invalid events
(missing `order_id`, `event_type`, or `OrderEvent` object) produce a structured
`NodeValidationError` and route to `FAIL_TO_HUMAN` — never an unhandled
`AttributeError`.

---

## 3. Policy Externalization

All business thresholds are centralised in `contracts/policy.py`.  No recipe,
node, or utility may hardcode a threshold value.

| Constant | Value | Injected into |
|---|---|---|
| `MAX_DISCOUNT_ALLOWED` | `0.15` (15%) | `PriceAdjustmentRecipe` via `erp_context` |
| `PRICE_CONDITION_TYPE` | `"YK07"` | `PriceAdjustmentRecipe` via `erp_context` |
| `CREDIT_AUTHORIZED_ROLES` | `("ORDER_MANAGER", "FINANCE_DIRECTOR")` | `CreditHoldReleaseRecipe` as param |
| `CREDIT_EXPOSURE_TOLERANCE` | `5_000.0` | `CreditHoldReleaseRecipe` as param |
| `DUPLICATE_PO_THRESHOLD_AUTO_BLOCK` | `0.90` | `DuplicatePORecipe` as param |
| `DUPLICATE_PO_THRESHOLD_REVIEW_REQUIRED` | `0.70` | `DuplicatePORecipe` as param |
| `DUPLICATE_PO_THRESHOLD_SOFT_FLAG` | `0.50` | `DuplicatePORecipe` as param |
| `MASS_UPDATE_LINE_COUNT_THRESHOLD` | `10` | `constraints/fallback_backend.py` |
| `CIRCUIT_BREAKER_MAX_UPDATES` | `50` | `orchestration/utils.py` |
| `CIRCUIT_BREAKER_MAX_VARIANCE` | `10_000.0` | `orchestration/utils.py`, `constraints/fallback_backend.py` |
| `DISCREPANCY_THRESHOLD` | `0.15` | `orchestration/utils.py` |

**Key audit property:** Recipes never import thresholds directly from `policy.py`.
All thresholds are injected by the orchestration layer (`validate_types` node),
so the same recipe logic can serve different customer / vendor threshold sets.
A threshold change requires modifying exactly one file (`contracts/policy.py`).

---

## 4. Infrastructure Gateways

### 4.1 Architecture

Infrastructure operations (SAP calls, database queries, external APIs) are
accessed through a **Gateway** layer that follows **Hexagonal Architecture
(Ports & Adapters)**:

| Component | File | Role |
|---|---|---|
| Protocol | `gateways/base.py` | `InfrastructureGateway` — typed interface (Port) |
| Registry | `gateways/registry.py` | Maps gateway names to adapter instances |
| Executor | `gateways/executor.py` | Wraps calls with tracing + error handling |
| Stub | `gateways/stub.py` | Test double — canned responses, no network |

**Gateway timeout enforcement:** `GatewayExecutor` enforces `timeout_ms` via
`concurrent.futures.ThreadPoolExecutor`.  A gateway that exceeds its deadline
receives a `TIMEOUT` response with structured error — never an infinite hang.
Exception handling uses two tiers: known types (`RuntimeError`, `ValueError`,
`TypeError`, `KeyError`) logged at `ERROR`, and unexpected types logged at
`CRITICAL` with `error_type` in the structured payload.

**Key invariant:** Recipes never call gateways directly.  The orchestration
layer resolves data before recipe execution (`resolve_dependencies` node) and
applies side effects after (`apply_effects` node).  This preserves recipe
purity and determinism.

### 4.2 Gateway Integration in the Graph

```
... → validate_types → resolve_dependencies → execute_recipe → apply_effects → END
```

- `resolve_dependencies` reads `RecipeSpec.dependencies` and calls gateways to
  fetch data the recipe needs.  Results are stored in `GraphState.resolved_data`.
  Gateway failure → `FAIL_TO_HUMAN`.
- `apply_effects` reads `RecipeSpec.effects` and calls gateways to apply
  side effects from recipe output.  Results are stored in
  `GraphState.effect_results`.  Effect failure is logged but does **not** undo
  the recipe result.

### 4.3 Typed Contracts

All gateway operations use typed `GatewayRequest` / `GatewayResponse` models
with `extra="forbid"`.  Response statuses are constrained to:
`SUCCESS`, `FAILED`, `TIMEOUT`, `UNAVAILABLE`.

---

## 5. Multi-Step Workflows (Saga Pattern)

### 5.1 Architecture

Multi-intent workflows are executed by `WorkflowRunner` (`workflows/runner.py`)
using the **Saga pattern** (Garcia-Molina & Salem, 1987):

- Each step runs through the **full graph** — intent classification, Compliance
  Shadow, recipe execution, gateway effects.
- If step N fails after steps 1..N-1 succeeded, compensation recipes are
  invoked in **reverse (LIFO) order**.
- Each step has its own independent Compliance Shadow audit.

### 5.2 Workflow Result Statuses

| Status | Meaning |
|---|---|
| `COMPLETE` | All steps succeeded |
| `FAILED` | A step failed; no compensation recipes declared |
| `COMPENSATED` | A step failed; compensation recipes were invoked for completed steps |
| `PARTIAL` | Reserved for future partial-completion modes |

### 5.3 Typed Contracts

`WorkflowDefinition`, `WorkflowStep`, `WorkflowStepResult`, `WorkflowResult`
— all use `extra="forbid"`.

---

## 6. Kill Switch

### 6.1 Purpose

The kill switch is an **emergency stop** that halts ALL automated recipe execution
before any graph node runs.  It is intended for:

- Production incidents where automated pricing changes must stop immediately.
- Maintenance windows.
- Operator-initiated pauses pending policy review.

### 6.2 Activation

```bash
export ASOE_KILL_SWITCH=1   # accepted values: 1, true, yes (case-insensitive)
```

No process restart required.  The check runs at each `run_graph()` call.

### 6.3 Behaviour

When active:

- `run_graph()` returns immediately — **zero nodes execute**.
- `final_status` = `FAIL_TO_HUMAN`
- `explanation` = `"Automated execution halted: ASOE_KILL_SWITCH is active. …"`
- The TraceRecord is still emitted to the observability log.

### 6.4 Deactivation

```bash
unset ASOE_KILL_SWITCH      # or set to 0 / false / no
```

### 6.5 Implementation reference

`hardening/kill_switch.py` — `is_kill_switch_active()`, `apply_kill_switch()`

---

## 7. Read-Only Explain Mode

### 7.1 Purpose

Explain mode is a **safe, read-only dry-run** for auditors and operators who want
to see what the system *would* do without committing any changes to SAP/ERP.

Intended for:

- Auditor impact assessments.
- Pre-production validation.
- CI smoke-tests in staging environments.
- Operator review of borderline orders.

### 7.2 Activation

```bash
export ASOE_EXPLAIN_MODE=1   # accepted values: 1, true, yes (case-insensitive)
```

### 7.3 Behaviour

When active, `run_graph()` uses `build_explain_graph()`, which replaces the
`execute_recipe` terminal node with `explain_only`:

| Node | Normal mode | Explain mode |
|---|---|---|
| `ingest` | runs | runs |
| `classify` | runs | runs |
| `load_skill` | runs | runs |
| `validate_circuit_breaker` | runs | runs |
| `shadow_audit` | runs | **runs** (real verdict) |
| `select_recipe` | runs | runs |
| `validate_types` | runs | runs |
| `resolve_dependencies` | runs | **skipped** |
| `execute_recipe` | runs | **replaced** by `explain_only` |
| `apply_effects` | runs | **skipped** |

The Compliance Shadow and circuit breaker both execute — the explanation
includes the **real** shadow verdict.

Terminal outcome:
- `final_status` = `MANUAL_REVIEW_REQUIRED`
- `explanation` = human-readable summary of intent, shadow verdict, recipe, and params

**No SAP writes.  No MCP calls.  No recipe side-effects.**

### 7.3 Implementation reference

`hardening/explain_mode.py` — `is_explain_mode_active()`, `build_explain_summary()`
`orchestration/nodes.py` — `explain_only()` node
`orchestration/graph.py` — `build_explain_graph()`

---

## 8. Circuit Breaker

Deployed in `orchestration/nodes.py` → `validate_circuit_breaker()`.

| Threshold | Limit | Action |
|---|---|---|
| Update count | > 50 per batch (`CIRCUIT_BREAKER_MAX_UPDATES`) | `FAIL_TO_HUMAN` |
| Total dollar variance | > $10,000 per batch (`CIRCUIT_BREAKER_MAX_VARIANCE`) | `FAIL_TO_HUMAN` |

Both thresholds are sourced from `contracts/policy.py` and evaluated on every
graph run.  A breach halts the graph **before** the shadow audit and recipe
selection.

Implementation: `orchestration/utils.py` → `circuit_breaker()`, thresholds in `contracts/policy.py`

---

## 9. Execution Invariants (Non-Negotiable)

The following invariants are enforced by code, not configuration.
Violating them requires modifying and re-reviewing source code.

| # | Invariant |
|---|---|
| 1 | No recipe runs unless `ComplianceDecision.status == GREEN` |
| 2 | No recipe runs unless `RecipeProposal.recipe_name` is in `AllowedRecipeName` |
| 3 | No recipe runs unless all required parameters are non-`None` |
| 4 | `ComplianceDecision.trace_id` propagates to `ExecutionLog.trace_id` unchanged |
| 5 | `GraphState.extra = "forbid"` — no untyped fields enter the state machine |
| 6 | Kill switch check precedes all node execution |
| 7 | Explain mode suppresses only `execute_recipe`; shadow always runs |
| 8 | `RecipeExecutor` has no `audit()`, `enforce()`, or `classify_intent()` methods |
| 9 | `SKILL.md` files are loaded verbatim — no summarisation or rewriting |
| 10 | All constrained outputs are validated by Pydantic before state advances |

---

## 10. Audit Trail

Every `run_graph()` call emits a `TraceRecord` (Phase 5 observability scaffold)
to the `asoe.observability` Python logger.  The record contains:

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

The record is JSON-serialisable and field-compatible with LangFuse trace schema
for future self-hosted forwarding.

---

## 11. Secret Management (Kubernetes)

Secrets are managed via the **Azure Key Vault CSI driver**, not environment
variables or ConfigMaps.

| Manifest | Purpose |
|---|---|
| `k8s/core/secret-provider.yaml` | `SecretProviderClass` that syncs Azure Key Vault secrets to a Kubernetes Secret (`asoe-secrets`) |
| `k8s/core/deployment.yaml` | Mounts the secrets-store volume and references `asoe-secrets` via `envFrom.secretRef` |

No credentials are hardcoded in source code, Dockerfiles, or environment
variable defaults.  Pods authenticate via Azure Workload Identity (temporary
tokens).

---

## 12. Environment Variable Reference

| Variable | Default | Description |
|---|---|---|
| `ASOE_KILL_SWITCH` | `0` | `1` / `true` / `yes` → halt all execution |
| `ASOE_EXPLAIN_MODE` | `0` | `1` / `true` / `yes` → dry-run only, no recipe execution |
| `USE_OUTLINES_BACKEND` | `0` | `1` → use `OutlinesConstrainedBackend` (requires `outlines` package) |

---

## 13. Test Coverage Reference

All hardening controls are covered by `tests/test_hardening.py`.
Golden regression tests for the full pipeline live in `tests/test_golden.py`.

Run the full suite:

```bash
python -m pytest
```

Expected outcome: **522 passed, 0 failed, 0 skipped.**

---

## 14. Local Execution Sandbox

The sandbox (`tests/sandbox/`) is an interactive tool for auditors and engineers
to run live pipeline executions without touching production systems.

| Component | Purpose |
|---|---|
| `tests/sandbox/seed.py` | Seeds a local SQLite database with sample SAP pricing, retailer contracts, credit profiles, and 8 EDI events covering all four intents |
| `tests/sandbox/ui/app.py` | Streamlit UI — select an event, run the full graph, inspect the step-by-step execution trace |
| `tests/sandbox/llm/local_backend.py` | Optional `LocalHFBackend` — Outlines constrained-JSON generation via a local HuggingFace model; falls back to `DeterministicFallbackBackend` if model unavailable |

**Key audit properties of the sandbox:**
- Every event runs through the full pipeline: classify → shadow → recipe → effects
- `ASOE_EXPLAIN_MODE=1` suppresses recipe execution (dry-run) — safe for auditor use
- `ASOE_KILL_SWITCH=1` halts all execution — visible in the environment banner
- The "Prompt Preview" expander shows the exact prompts sent to the constrained backend
- The "Full JSON trace" expander exposes the complete serialised `GraphState`

The sandbox database (`.db`) is git-ignored; only the seeder script is committed.
