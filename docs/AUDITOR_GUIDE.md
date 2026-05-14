# Auditor Guide — CPG Agentic Pricing Exception System

**Document owner:** Principal AI Systems Architect
**Scope:** Hardening controls, constrained-generation safeguards, API security (JWT, RBAC, tenant isolation), database audit controls, and operational safety
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
| **Allowed values** | `CONTRACTUAL_CORRECTION`, `CREDIT_BLOCK`, `MASS_PRICING_ERROR`, `DUPLICATE_PO`, `PRICE_HOLD_RELEASE`, `EDI_MISMATCH`, `BACK_ORDER`, `OVER_MAX`, `MIN_ORDER_QTY`, `PALLET_CONFIG`, `DELIVERY_DELAY` |
| **Enforcement** | `AllowedIntent` Pydantic `Literal` type; Pydantic raises `ValidationError` on any other value |
| **Routing fork** | `EDI_850_LINE_MISMATCH` events whose `metadata.mismatch_sub_type == "PRICE_MISMATCH"` are classified as `CONTRACTUAL_CORRECTION` (not `EDI_MISMATCH`), preserving `PriceAdjustmentRecipe.py` as the single source of truth for pricing. Enforced in `constraints/fallback_backend.py:classify_intent` and mirrored in `skills/loader.py:select_for_event`. |
| **Backend** | `DeterministicFallbackBackend` (no LLM in CI/test) or `OutlinesConstrainedBackend` (production Outlines regex) |
| **Fallback chain** | Custom backend → `OutlinesConstrainedBackend` → `DeterministicFallbackBackend` (graceful degradation with `logger.warning()` on each failure) |

Any value outside the allowed set causes a `ValidationError` before the state
machine advances.  The system routes to `FAIL_TO_HUMAN`.

#### Confidence — the real classifier value, not a synthesised default

`AnalysisResponse.confidence` (the 0-100 integer surfaced on the
operator's Agent Recommendation card and on `GET /api/v1/exceptions/{id}/analysis`)
is the **real** classifier output, not a fabricated mid-range default.

* **Source:** the active backend's `IntentDecision.confidence` (a 0.0-1.0
  float; `DeterministicFallbackBackend` produces 0.80-0.95 by intent;
  remote LLMs produce whatever the model returns).
* **Persistence:** `state.confidence` is written to `trace_data["intent_confidence"]`
  at every `/resolve` and `/reanalyze` write site
  (`api/routes/exceptions.py` ~L229 and ~L1245).
* **Read:** `GET /api/v1/exceptions/{id}/analysis` scales the persisted
  float to a 0-100 int, clamped via `max(0, min(100, ...))`. A missing,
  zero, or negative value returns 0 — never a fabricated mid-range
  default.
* **Cross-check:** when LLM and deterministic classifiers disagree, the
  graph forces `final_status = MANUAL_REVIEW_REQUIRED` and
  `state.confidence = check.winning_decision.confidence` (the
  deterministic decision, per `constraints/cross_check.py`). The audit
  trail records both intents on the `LLMCallTrace` so the disagreement
  is reconstructible.

This is a Verdict 2026-04-22 / Guardrail #6 commitment: the operator
must be able to distinguish a real classifier output from a synthesised
default. The legacy `confidence = 80 if intent_selected else 0`
hardcode (the source of the deployed-system "every record at 80%"
appearance) is removed; its regression is locked by the four tests in
`tests/test_analysis_confidence_persistence.py`.

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
| **Allowed values** | `PriceAdjustmentRecipe.py`, `CreditHoldReleaseRecipe.py`, `DuplicatePORecipe.py`, `PriceHoldReleaseRecipe.py`, `EdiMismatchRecipe.py`, `BackOrderResolutionRecipe.py`, `OverMaxTrimRecipe.py`, `MOQRoundUpRecipe.py`, `PalletAlignmentRecipe.py`, `DeliveryDelayResolutionRecipe.py` |
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
| `PRICE_HOLD_TOLERANCE_PCT` | `0.02` (2%) | `PriceHoldReleaseRecipe` as `tolerance_pct` param |
| `PRICE_HOLD_HARD_BLOCK_PCT` | `0.10` (10%) | `PriceHoldReleaseRecipe` as `hard_block_pct` param |
| `EDI_MISMATCH_AUTONOMY_LEVELS` | `{SKU: L3, QTY: L2, UOM: L2, SHIP_TO: L1}` | `EdiMismatchRecipe` as `autonomy_levels` param |
| `BACK_ORDER_SEVERE_GAP_PCT` | `0.50` (50%) | `BackOrderResolutionRecipe` as `severe_gap_pct` param (SD-OOS-002) |
| `OVER_MAX_SEVERE_EXCEEDANCE_PCT` | `0.50` (50%) | `OverMaxTrimRecipe` as `severe_exceedance_pct` param (SD-OM-002) |
| `MOQ_SEVERE_SHORTFALL_PCT` | `0.25` (25%) | `MOQRoundUpRecipe` as `severe_shortfall_pct` param (SD-MOQ-002) |
| `MOQ_UPLIFT_REVIEW_PCT` | `0.10` (10%) | `MOQRoundUpRecipe` as `uplift_review_pct` param |
| `PALLET_CONFIG_MIN_FILL_PCT` | `0.90` (90%) | `PalletAlignmentRecipe` as `min_fill_pct` param (SD-PLT-002) |
| `PALLET_CONFIG_BROKEN_LAYER_FILL_PCT` | `1.00` (100%) | `PalletAlignmentRecipe` as `broken_layer_fill_pct` param (SD-PLT-001) |
| `DELIVERY_DELAY_MINOR_DAYS` | `2` | `DeliveryDelayResolutionRecipe` as `minor_days` param (SD-DELAY-001) |
| `DELIVERY_DELAY_SEVERE_DAYS` | `5` | `DeliveryDelayResolutionRecipe` as `severe_days` param (SD-DELAY-002) |
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
| `llm_shadow_verdict_action` | ADR-039 §6.3 — the L2 LLM Shadow's action (`AGREE` / `DISAGREE_DOWNGRADE` / `ABSTAIN`) when L2 was invoked; `null` when gating skipped L2. Read by the override-handler post-commit to drive the X.2 → X.3 ratification SLI (see §11 Observability surface). |
| `recipe_name` | Selected recipe filename (or `null`) |
| `rag_chunks` | Reserved for V2 RAG integration — always empty in V1.0 (forward-compatible field) |
| `constrained_output_schemas` | Map of layer → schema name (e.g. `intent → IntentDecision`) |
| `gateway_calls` | Gateway operations invoked (dependency resolutions + effect applications) |
| `final_status` | `COMPLETE`, `FAIL_TO_HUMAN`, `BLOCKED`, `MANUAL_REVIEW_REQUIRED`, `REJECTED` |
| `explanation` | Human-readable reason for the terminal decision |

The record is JSON-serialisable and field-compatible with LangFuse trace schema.

### HITL Audit Events (Phase 19 — Override Action consolidation)

Human-in-the-loop actions on an exception are logged to `policy_audit_log`
under the following `policy_key` values. Each row carries
`previous_value` (the pre-action snapshot), `new_value` (the post-action
payload), `changed_by` (the JWT `sub` of the caller — never
client-supplied), and `change_reason` (the caller's notes).

| `policy_key` | Emitted by | Notes |
|---|---|---|
| `EXCEPTION_RESOLVED` | `PATCH /exceptions/{id}/disposition` | `new_value.sub_type` is one of `APPROVE`, `REJECT`, `OVERRIDE`. A single SQL query against this event answers "how often do humans deviate from the agent?" |
| `EXCEPTION_ESCALATED` | `POST /exceptions/{id}/escalate` | Routing-only event (no resolution asserted). Own permission: `exceptions:escalate` (analyst+) |
| `EXCEPTION_OVERRIDE_INITIATED` | `/disposition` when chosen ≠ recommended AND `financial_impact_usd >= HIGH_VALUE_OVERRIDE_THRESHOLD_USD` | Lifecycle transitions to `PENDING_COSIGN`. The pending action is stashed on `resolution_data.pending_override` |
| `EXCEPTION_OVERRIDE_COSIGNED` | `POST /exceptions/{id}/override/cosign` with `approve=true` | Applies the pending override (lifecycle → `RESOLVED`). `new_value` includes both `initiator` and `cosigned_by` |
| `EXCEPTION_OVERRIDE_REJECTED` | `POST /exceptions/{id}/override/cosign` with `approve=false` | Restores the prior `lifecycle_state` stashed on `pending_override.from_lifecycle_state` |

The legacy `EXCEPTION_OVERRIDE`, `EXCEPTION_APPROVE`, and
`EXCEPTION_REJECT` event types were **retired in Phase 19** along with
the per-verb endpoints that produced them. Historical rows carrying
those keys remain immutable in the chain; new writes use the events
above.

### LangFuse Forwarding (Optional)

When `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set and the `langfuse`
package is installed, `Tracer.emit()` forwards each `TraceRecord` to LangFuse
in addition to stdlib logging.  This creates a LangFuse trace per graph execution
with one span per LangGraph node that ran (`ingest`, `classify`, `load_skill`,
`validate_circuit_breaker`, `select_recipe`, `resolve_dependencies`,
`validate_types`, `shadow_audit`, `execute_recipe`, `apply_effects`,
`build_analysis` — see `architecture_v4.md §5.1` for the topology), per-LLM-call
`generation` observations attached as children of their owning step span (so
auditors expand `classify` and see the LLM call inline), and a
`terminal_status` score that enables dashboard filtering and success-rate
tracking:

| `final_status` | Score `value` | Meaning |
|---|---|---|
| `COMPLETE` | **1.0** | Recipe executed successfully |
| `FAIL_TO_HUMAN` | 0.0 | Escalated to human (circuit breaker, missing params, gateway failure) |
| `MANUAL_REVIEW_REQUIRED` | 0.0 | Shadow returned YELLOW — requires review |
| `BLOCKED` | 0.0 | Shadow returned RED — halted by policy |
| `REJECTED` | 0.0 | Rejected by policy |

The score's `comment` field contains the exact `final_status` string, allowing
auditors to distinguish failure reasons when filtering traces with `value=0.0`.

**Stdlib logging remains the authoritative audit record.**  LangFuse is additive
— all forwarding errors are caught and logged at WARNING/DEBUG level; they never
block execution.  LangFuse keys are managed via Azure Key Vault CSI in production
(`k8s/core/secret-provider.yaml`).

Implementation: `observability/langfuse_sink.py`.
Full integration specification: `prompts/phase_10_langfuse.md`.

### Real-Time Event Publishing (WebSocket)

In addition to the audit trail, every graph execution publishes a `task_complete`
event to a per-tenant Redis Pub/Sub channel (`asoe:ws:{tenant_id}`). Authenticated
WebSocket clients connected to `ws://host/api/v1/ws` receive these events in
real time. Events include `trace_id`, `exception_id`, `final_status`, and
`explanation` — enabling live dashboards and monitoring without polling.

**Tenant isolation:** Each WebSocket client subscribes only to their tenant's
channel. Authentication is via JWT (first message must be `{ "type": "auth",
"token": "eyJ..." }`). A 60-second replay buffer allows reconnecting clients to
catch up on missed events.

**Failure isolation:** Redis publish failures are logged at WARNING and never
block graph execution or API responses. Clients can poll
`GET /api/v1/exceptions/{id}` as a fallback.

Implementation: `api/events.py` (event schemas), `api/pubsub.py` (pub/sub manager),
`api/routes/ws.py` (WebSocket hub).

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

## 12. API Security Controls

The FastAPI API layer (`api/`) implements defence-in-depth per architecture_v3.md §11.

### 12.1 Authentication (§11.1)

All protected endpoints require a JWT Bearer token in the `Authorization` header.

| Property | Value |
|---|---|
| **Algorithm** | HS256 (dev); production uses Key Vault-managed secret via `ASOE_JWT_SECRET` |
| **Access token lifetime** | 15 minutes (`exp` claim validated on every request) |
| **Refresh token lifetime** | 7 days (`token_type: "refresh"`) |
| **Refresh rotation** | Each refresh call issues a new access + new refresh token |
| **MFA enforcement** | Login flow always returns `mfa_required: true`; MFA code required before tokens are issued |
| **`auth_method` claim** | `"password+mfa"` for login, `"sso"` for SSO — enables audit differentiation |

**Expired tokens return 401.** Refresh endpoint rejects access tokens (validates `token_type` claim).

### 12.2 RBAC (§11.2)

| Role | Key Permissions | Enforcement |
|---|---|---|
| `analyst` | `exceptions:read`, `exceptions:approve` | FastAPI dependency injection |
| `manager` | analyst + `exceptions:override`, `rules:write` | FastAPI dependency injection |
| `admin` | manager + `users:manage`, `policy:write`, `audit:read` | FastAPI dependency injection |
| `viewer` | `exceptions:read`, `dashboard:read` | FastAPI dependency injection |
| `partner` | `exceptions:read` (scoped to own orders) | FastAPI dependency + `retailer_id` filtering |

### 12.3 Tenant Isolation (§11.3)

**Two-layer enforcement:**

1. **Application layer:** `tenant_id` extracted from JWT `org` claim and injected into every database query. Tests verify no query omits the `tenant_id` predicate.
2. **Database layer (PostgreSQL):** Row-Level Security policies on `exceptions`, `traces`, `policy_overrides`, `checkpoints`. The connection adapter sets `app.current_tenant_id` session variable. RLS returns zero rows when the variable is unset (misconfiguration guard).

**Partner-role scoping:** Partners with `retailer_id` claim see only their own orders in list endpoints. Partners are blocked from resolve, override, approve, reject, and trace endpoints.

**RLS test coverage:** Row-Level Security is verified by integration tests on real PostgreSQL (`tests/test_postgres.py` — `TestRowLevelSecurity`). Tests confirm RLS is enabled on `exceptions` and `traces` tables, that Tenant A cannot see Tenant B's rows via the repository, and that GET by ID with wrong tenant returns `None`. These tests require `ASOE_TEST_POSTGRES_URL` and are skipped in CI when not set.

### 12.4 Environment Isolation (§11.6)

JWT `env` claim is validated against `ASOE_ENV` environment variable on every authenticated request. A sandbox token presented to a production server returns **403 with generic "Access denied."** — no internal state, stack traces, or exception metadata leaked in the response.

### 12.5 Trace ID Propagation (§11.4)

Every request/response includes an `X-Trace-ID` header. Client-provided values are propagated unchanged; missing values generate a UUID at the API boundary. The trace ID flows through `ComplianceDecision.trace_id` → `ExecutionLog.trace_id` → `TraceRecord.trace_id` (Execution Invariant #4).

Implementation: `api/middleware.py` → `TraceIDMiddleware`.

---

## 13. Database Audit Controls

### 13.1 Policy Audit Log (SOX Requirement)

Every policy override change is immutably logged in the `policy_audit_log` table **before** the new value takes effect.

| Field | Description |
|---|---|
| `previous_value` | Value before the change (NULL for first override) |
| `new_value` | New value being applied |
| `changed_by` | JWT `sub` claim of the admin making the change |
| `change_reason` | Free-text reason for the change |

**Immutability enforcement:** A PostgreSQL trigger (`trg_policy_audit_immutable`) raises an exception on any `UPDATE` or `DELETE` attempt. Additionally, `REVOKE UPDATE, DELETE` is applied for the `asoe_app` and `asoe_worker` roles.

**Test coverage:** The SOX immutability trigger is verified by integration tests on real PostgreSQL (`tests/test_postgres.py` — `TestSOXAuditImmutability`). Tests confirm that INSERT succeeds while UPDATE and DELETE are blocked by the trigger. These tests require a running PostgreSQL instance and are skipped in CI when `ASOE_TEST_POSTGRES_URL` is not set.

### 13.1.1 Hash-Chained Append-Only Audit Log (Phase 20)

On top of the immutability trigger above, `policy_audit_log` is a
**tamper-evident hash chain** at both the application and the database
layers.

| Property | Value |
|---|---|
| **Columns added** | `prev_hash TEXT NOT NULL DEFAULT 'GENESIS'`, `event_hash TEXT NOT NULL` |
| **Hash function** | `sha256(prev_hash || "|" || canonical_json(row))` — identical in Python (`api/store.py`, `db/repository.py`) and SQL (`V003` migration, pgcrypto `digest()`) |
| **Tenant isolation** | Each `tenant_id` has its own chain rooted at `GENESIS` — no cross-tenant contamination is possible |
| **Append-only enforcement** | `BEFORE UPDATE` and `BEFORE DELETE` triggers raise `policy_audit_log is append-only; <OP> rejected (drop trigger to override)` |
| **Verification API** | `PolicyRepository.verify_audit_chain(tenant_id)` (DB backend) and `ExceptionStore.verify_audit_chain(tenant_id)` (in-memory backend) — both return `(True, None)` on a clean chain or `(False, first_break_idx)` on any tampering |
| **Migration** | `db/migrations/V003__audit_hash_chain.sql` — adds columns, backfills a valid chain across existing rows in `(tenant_id, created_at, id)` order, installs the triggers. Idempotent. |

**Why this matters:** the single `REVOKE` + trigger in V001 already
prevented casual edits, but an operator with `DROP TRIGGER` privilege
could mutate a row and re-enable the trigger. The chain makes such a
mutation visible: any row whose recomputed `event_hash` no longer
matches breaks the chain from that index onwards, and
`verify_audit_chain` reports the break position.

**Test coverage:** `tests/test_audit_chain.py` (6 tests — in-memory) and
`tests/test_db_audit_chain.py` (8 tests — SQLite + optional PostgreSQL).
Includes a cross-implementation parity test that locks in identical
hashes between `api/store.py`, `db/repository.py`, and the V003
migration backfill.

### 13.2 Schema Security

- All tables use `extra="forbid"` equivalent CHECK constraints
- Intent values constrained to the `AllowedIntent` enum at the database level
- `context_embedding VECTOR(1536)` column installed but not indexed (V2 readiness)
- `schema_migrations` table tracks applied versions

Implementation: `db/migrations/V001__initial_schema.sql`.

---

## 14. V1 Foundation Guardrails (CI-Enforced)

Six guardrails are enforced by automated tests in CI (architecture_v3.md §15).
Violating any guardrail fails the build. These preserve the V2/V3 expansion
path by preventing V1 code from accumulating technical debt.

| # | Guardrail | Enforcement | Test File |
|---|---|---|---|
| 1 | **No intent-specific logic in pipeline nodes** | AST inspection of `orchestration/nodes.py` — no `if/elif` branches on intent string literals | `test_v1_guardrails.py` |
| 2 | **Dynamic enum serving** | Health endpoint serves `allowed_intents` and `allowed_recipes` from schema, not hardcoded lists | `test_v1_guardrails.py` |
| 3 | **Metadata keys documented per RecipeSpec** | Each recipe declares `expected_metadata_keys`; test fixtures include all declared keys | `test_v1_guardrails.py` |
| 4 | **ERP-agnostic gateway protocol** | Code-only scan (docstrings excluded) for BAPI/RFC/SAP/Oracle/Dynamics terms in `gateways/` | `test_v1_guardrails.py` |
| 5 | **Intent-agnostic exceptions table** | Column introspection: no similarity/damage/deduction columns; `resolution_data` is the only extensibility point | `test_v1_guardrails.py` |
| 6 | **Hierarchical policy key format** | Regex validation: `global.*`, `tenant.{id}.*`, `retailer.{id}.*` — flat keys rejected | `test_v1_guardrails.py` |

**Why this matters for auditors:** These guardrails ensure that adding a new
intent (e.g., `SHORT_SHIP` in V1.5) requires zero changes to the pipeline,
API, gateway protocol, or database schema. All intent-specific logic lives
exclusively in recipes and skill documents.

---

## 15. Environment Variable Reference

| Variable | Default | Description |
|---|---|---|
| `ASOE_KILL_SWITCH` | `0` | `1` / `true` / `yes` → halt all execution |
| `ASOE_EXPLAIN_MODE` | `0` | `1` / `true` / `yes` → dry-run only, no recipe execution |
| `ASOE_ENV` | `sandbox` | `sandbox` or `production` — JWT `env` claim must match (mismatch → 403) |
| `ASOE_JWT_SECRET` | _(dev fallback)_ | JWT signing secret — **required for production** |
| `ASOE_ACCESS_TOKEN_TTL_SECONDS` | sandbox `86400` (24h) / production `3600` (60min) | Access-token lifetime. Empty / malformed / zero / negative values fall back to the per-`ASOE_ENV` default (`api/deps.py::_resolve_token_ttls`). Bicep param `accessTokenTtlSeconds`. |
| `ASOE_REFRESH_TOKEN_TTL_SECONDS` | sandbox `2592000` (30d) / production `604800` (7d) | Refresh-token lifetime. Same defensive resolution as access TTL. Bicep param `refreshTokenTtlSeconds`. |
| `DATABASE_URL` | _(unset)_ | PostgreSQL connection; when set, API uses database-backed persistence |
| `USE_OUTLINES_BACKEND` | `0` | `1` → use `OutlinesConstrainedBackend` (requires `outlines` package) |

---

## 16. Test Coverage Reference

All hardening controls are covered by `tests/test_hardening.py`.
Golden regression tests for the full pipeline live in `tests/test_golden.py`.

Run the full suite:

```bash
python -m pytest
```

Expected outcome: **1007 passed, 0 failed.**

PostgreSQL integration tests (35 tests in `tests/test_postgres.py`) require a running PostgreSQL instance and `ASOE_TEST_POSTGRES_URL`. They are skipped automatically when the variable is unset.

---

## 17. Local Execution Sandbox

The sandbox (`tests/sandbox/`) is an interactive tool for auditors and engineers
to run live pipeline executions without touching production systems.

| Component | Purpose |
|---|---|
| `tests/sandbox/seed.py` | Seeds a local SQLite database with customers, distribution centres, promotions, SAP pricing, retailer contracts, credit profiles, and 18 EDI events covering all four intents |
| `tests/sandbox/ui/app.py` | Streamlit UI — select an event, run the full graph, inspect the step-by-step execution trace |
| `tests/sandbox/llm/local_backend.py` | Optional `LocalHFBackend` — Outlines constrained-JSON generation via a local HuggingFace model; falls back to `DeterministicFallbackBackend` if model unavailable |

**Key audit properties of the sandbox:**
- Every event runs through the full pipeline: classify → shadow → recipe → effects
- `ASOE_EXPLAIN_MODE=1` suppresses recipe execution (dry-run) — safe for auditor use
- `ASOE_KILL_SWITCH=1` halts all execution — visible in the environment banner
- The "Prompt Preview" expander shows the exact prompts sent to the constrained backend
- The "Full JSON trace" expander exposes the complete serialised `GraphState`

The sandbox database (`.db`) is git-ignored; only the seeder script is committed.

---

## 18. HITL Governance Controls (Phase 19 — Override Action consolidation)

The four controls below wrap every human disposition on an exception.
They are enforced server-side by `api/routes/exceptions.py` and backed
by the hash-chained audit log in §13.1.1.

### 18.1 Segregation of Duties

| Property | Value |
|---|---|
| **Where** | `POST /exceptions/{id}/override/cosign` (cosign self-block only). |
| **Rule** | On a cosign, the caller's `user.sub` must not equal `resolution_data.pending_override.initiator`. |
| **Failure mode** | `403 SOD_VIOLATION` naming the cosign boundary. Idempotency-Key lookups run **before** the SoD check so a retry of a successful first call still returns the cached success. |
| **Scope note (PO ruling 2026-05-03)** | The earlier self-block on `PATCH /exceptions/{id}/disposition` (a user could not run a second OVERRIDE on a record they themselves resolved) was **removed**. Operators legitimately need to correct their own earlier overrides without escalation churn. The audit trail still records every override attempt — initiator, timestamp, reason_tag, action — via `reanalysis_history`, so SOX evidence-of-control is preserved. The four-eyes high-value override rule in §18.2 remains the SOX §404 control of record. Regression locked by `tests/test_override_escalate.TestSegregationOfDuties.test_same_user_can_override_own_resolution` and the asoe-ui `tests/browser/override-and-sod.spec.ts` Playwright spec. |

### 18.2 Four-Eyes High-Value Override

| Property | Value |
|---|---|
| **Threshold** | `HIGH_VALUE_OVERRIDE_THRESHOLD_USD` in `contracts/policy.py` (default `10_000.0`) — externalised, not hardcoded in a handler. |
| **Impact source** | Extracted from `record.resolution_data` under any of `financial_impact_usd`, `financial_impact`, `impact_usd`. Absent/non-numeric impact → four-eyes does **not** fire (we never block on unmeasurable materiality). |
| **Staging** | A `/disposition` call whose derived `sub_type == OVERRIDE` and whose impact meets or exceeds the threshold transitions the record to `PENDING_COSIGN` and stashes `{ action, notes, reason_tag, initiator, initiated_at, financial_impact_usd, from_lifecycle_state }` on `resolution_data.pending_override`. |
| **Cosign endpoint** | `POST /exceptions/{id}/override/cosign` with `{ approve: bool, notes: str }`. `approve=true` applies the pending override (lifecycle → `RESOLVED`, `resolved_by = initiator`, `cosigned_by = caller`); `approve=false` restores `pending_override.from_lifecycle_state`. Notes are mandatory in both cases. |
| **Audit event stream** | `EXCEPTION_OVERRIDE_INITIATED` → (either) `EXCEPTION_OVERRIDE_COSIGNED` or `EXCEPTION_OVERRIDE_REJECTED`. All three are hash-chained. |
| **Standards** | SOX §404 management-override control: two reviewers required above the materiality threshold. |

### 18.3 Hash-Chained Append-Only Audit Log

See §13.1.1 — `prev_hash` + `event_hash` columns, `BEFORE UPDATE` /
`BEFORE DELETE` triggers raising `policy_audit_log is append-only`,
`verify_audit_chain()` at both the application and DB layers,
per-tenant chains rooted at `GENESIS`, and the V003 migration that
installs everything idempotently.

### 18.4 Idempotency

| Property | Value |
|---|---|
| **Where** | `PATCH /exceptions/{id}/disposition`, `POST /exceptions/{id}/escalate`, `POST /exceptions/{id}/override/cosign` |
| **Header** | `Idempotency-Key: [A-Za-z0-9_-]{1,128}`. Malformed values return `422 INVALID_IDEMPOTENCY_KEY`. |
| **Cache key** | `(tenant_id, exception_id, user.sub, key)` |
| **TTL** | 24 hours, in-memory, per-process (single-replica deployment). Phase 3+ migrates this to Redis for multi-replica safety. |
| **Conflict** | Reusing the same key with a different request body returns `409 IDEMPOTENCY_CONFLICT`. |
| **Ordering** | Idempotency lookup runs before state-machine and SoD guards, so a retry of a successful first call returns the cached response rather than being rejected by a transition that the first call already completed. |

### 18.5 Trust-Boundary Fix (Phase 19)

`resolved_by` is **never** client-supplied. All HITL endpoints derive
it from `user.sub` on the JWT. The legacy `OverrideRequest.resolved_by`
field was removed as part of the `/disposition` consolidation —
spoofing the auditor identity is no longer representable in the wire
format.

---

## 19. Case-Centric Audit Controls (ADR-038 — Proposed; Phase H.1 → H.7
primitives shipped)

ADR-038 introduces a parent `OrderCase` entity above the existing
`ExceptionRecord` lifecycle. The audit-control implications are
recorded here ahead of full integration so auditors and operators
have the contract in writing.

### 19.1 OrderCase parent entity

| Field | Audit relevance |
|---|---|
| `case_id` | Stable identifier for every materialised case. Surfaces in trace logs and any case-detail UI link. Backfill (V011) derives a deterministic id (`sha256(tenant ‖ order_id)[:16]`) so re-running the migration is idempotent. |
| `source` | `manual_order` (email / phone / fax — eager case open) vs `automated_order` (EDI / portal / B2B API — lazy case open on first non-clean event). Set at case-open and **immutable**. Answers "how did the order originate?". |
| `case_type` (ADR-041) | `EMAIL_ENTRY` (customer email arrived) vs `BLOCK` (SAP order carried a block reason). **Orthogonal to `source`** per the domain modeller's panel review — a manual_order arriving by phone is NOT EMAIL_ENTRY; an automated_order (EDI 850) that gets SAP-blocked IS BLOCK. Set at case-open and immutable. Defaults from `source` via `contracts/models.py::infer_case_type` for back-compat with pre-ADR-041 call sites. |
| `email_classification` (ADR-041) | One of `NEW_ORDER` / `ORDER_CHANGE` / `INQUIRY` / `COMPLAINT` / `OTHER`. **Required** when `case_type == "EMAIL_ENTRY"` (defaults to `OTHER`); **must be None** when `case_type == "BLOCK"`. Enforced at construction by `OrderCase._check_case_type_invariants` (Pydantic `mode="after"` validator). 1:1 with the intake email. |
| `source_channel` | Channel sub-classification (`edi_x12_850`, `email`, `portal`, `phone`, …). Case-level metadata; does **not** alter intent or recipe selection. |
| `customer_po_number` / `sales_order_id` / `edi_transaction_id` / `source_email_id` | Correlation keys. The lookup-or-create policy (`api/store.py::CaseStore.lookup_or_create`) checks these in **SO → PO → EDI → email** priority. Emitting two records with the same SO opens **one** case, never two. The hard invariants "EMAIL_ENTRY ⇒ source_email_id required" + "BLOCK ⇒ sales_order_id required" are deferred per ADR-041 §5 — soft today, hard once the ingestion-path audit lands. |
| `tier` | `1` = stateless / `2` = stateful (default for non-clean events) / `3` = compacted. Tier graduation is monotonic — a case never downgrades. |
| `bundle_version_at_open` | The L0 bundle version stamp at case-open. Backfilled cases carry the sentinel `legacy-pre-h2`. |
| `sla_deadline` | ISO timestamp computed via `agents/sla.py::stamp_sla_deadline()` from `knowledge/policy/sla_per_customer_tier.yaml`. Defaults to 48h when the customer's tier isn't in the table. |

The `parent_case_id` column on `ExceptionRecord` (V009) is nullable
in the persistence model, but ADR-041 §2.2 makes `should_materialise()
-> True` unconditional — every newly-persisted record gets a parent
case. Existing legacy records remain `NULL` until the V011 backfill
runs.

**ExceptionRecord audit-bearing field added by ADR-041:**

| Field | Audit relevance |
|---|---|
| `sap_block_code` | Raw SAP block reason code on records whose parent case is `case_type == "BLOCK"` (1:N — one SAP order can carry multiple simultaneous codes). Distinct from `intent` (the classified business-intent vocabulary recipes dispatch on). `None` on EMAIL_ENTRY-parented records. Surfaces in trace logs and the inline `ExceptionDetailPanel` evidence rows. |

**Detail-path visibility symmetry (ADR-041 §2.2):** `GET /api/v1/cases/{id}`
and `GET /api/v1/cases/{id}/records` are **tenant-scoped only** on the
detail path — no `_scope_to_user` (assigned-accounts) filter. Pre-
ADR-041 the case-detail endpoint applied account scoping while the
sibling `GET /api/v1/exceptions/{id}` was tenant-only; an analyst
could read a child record but get 404 on the parent case, breaking
deep-link audit trails. Symmetry restored; locked at
`tests/test_routes_cases.py::TestDetailVisibilityInvariant`
(parametrized across analyst / manager / viewer / assigned-analyst /
partner — every exception visible to a user implies the parent case
is too). The list endpoint (`GET /api/v1/cases`) retains the
account-scope filter as a UX queue-curation aid; only the detail
path is tenant-only.

### 19.2 Correlation table (V010)

| Element | Audit relevance |
|---|---|
| `(tenant_id, key_type, key_value)` | Composite primary key. Tenant scoping is enforced at this layer; the same `customer_po_number` belonging to two tenants opens two separate cases. |
| `key_type` | One of `sales_order_id`, `customer_po_number`, `edi_transaction_id`, `source_email_id`. Priority order is encoded in `CaseStore.lookup_or_create`. |
| `registered_at` | Append-only audit field. Re-registration of an existing key is a no-op (`ON CONFLICT DO NOTHING`). |

### 19.3 Lazy materialisation policy (Phase H.3)

Materialisation is delegated to `api/case_resolver.py::materialise_for_event`,
called from `api/routes/exceptions.py::_persist_exception`:

* **Manual Orders** open a case eagerly — every event materialises a
  case immediately, regardless of terminal status.
* **Automated Orders** open lazily — case is materialised only when the
  pipeline's `final_status` is non-clean (anything other than
  `COMPLETE`). Clean COMPLETE Automated records persist with
  `parent_case_id = NULL` and are not visible on the case surface.

The intent here is operational: most automated traffic resolves
without any human touch. Materialising a case for every clean record
would inflate the case queue with nothing actionable. The audit trail
for clean automated traffic remains the existing `ExceptionRecord`
row plus its hash-chained audit entry; nothing about the existing
audit log is lost.

### 19.4 Compaction protocol (ADR-038 §7.4 — pending Compliance ratification)

Compaction is **deterministic, not LLM-driven** (§7.4 binding).

* Triggers (`agents/compaction.py::CompactionTrigger.evaluate`):
  8,000-token working-context budget exceeded **OR** 25 events on
  the case **OR** 7 days since case-open.
* Templates: per-event-type Markdown templates under
  `knowledge/compaction/<event_type>.template.md`. Currently only
  the fallback `__general__.template.md` ships; per-event-type
  authorship is part of Compliance + domain SME work that comes
  with §7.4 ratification.
* Output cap: 2,000 tokens per compaction summary. Original events
  are **retained verbatim** — compaction summaries are appended,
  not destructive.
* Replay-divergence target: **0%**. Re-running compaction on the
  same event sequence yields the same summary string, and the
  test suite enforces this in `tests/test_compaction_sla_backfill.py`.

This determinism matters for SOX replay: the case's narrative
context can be regenerated bit-identical from the raw event log
even if the original summary is corrupted or lost.

### 19.5 SLA tracking (Phase H.7)

| Element | Audit relevance |
|---|---|
| `knowledge/policy/sla_per_customer_tier.yaml` | The L0 policy artefact. Strategic 4h / Mid-Market 24h / Long-tail 72h / default 48h. Editable through the standard L0 review flow (Compliance + Product + customer-success). |
| `agents/sla.py::stamp_sla_deadline()` | Pure function — same input always yields the same deadline. Used at case-open to populate `OrderCase.sla_deadline`. |
| `agents/sla.py::reload_policy()` | Test + Compliance-workshop hot-reload entry point. Runtime policy changes are tracked in git, not in a database. |

### 19.6 Backfill (V011 + `agents/backfill.py`)

For deployments that already have legacy `ExceptionRecord` rows with
`parent_case_id = NULL`:

* **Pass 1** (`backfill_orphan_cases`): one case per orphan record.
  `case_id` is derived deterministically so re-running the migration
  is idempotent. The Python in-memory companion and the Postgres SQL
  agree on the policy.
* **Pass 2** (`merge_orphan_cases_by_correlation`, optional): merges
  cases sharing `(tenant_id, customer_po_number)`. Requires a
  maintenance window because case_ids change. Not part of the V011
  migration — operators run it explicitly via `dry_run=True` first.

Pass 1 is safe to run unattended; Pass 2 requires Compliance sign-off
because it changes case references and downstream UI URLs.

### 19.7 What is **not yet** in scope for the auditor

The following ADR-038 items are still pending and should not be
relied upon for audit evidence yet:

* The Case Agent itself runs only in unit tests. Production traffic
  continues on the deterministic graph; `parent_case_id` is set but
  the agent does not yet act on cases.
* ~~The `/api/v1/cases/*` HTTP route does not exist.~~ **Shipped.**
  `api/routes/cases.py` exposes `GET /api/v1/cases` (list, with cursor
  pagination — ADR-038 §D7), `GET /api/v1/cases/{id}` (detail —
  tenant-scoped only per ADR-041 §2.2), and
  `GET /api/v1/cases/{id}/records` (attached records, with aggregated
  policy hits). The asoe-ui `/cases` workspace consumes these
  directly when `NEXT_PUBLIC_USE_REAL_API=1`.
* L4 harness extensions (case-aware concurrency lock, tool-call
  replay log, tier graduation hook) are not yet visible in the
  codebase.
* Four-eyes / cosign / override flows still operate on the
  exception lifecycle (ADR-029); migration to the case lifecycle
  is part of Phase H.7 closeout. ADR-040 (case-level cosign) has
  the X.0 code path in place behind `ASOE_CASE_COSIGN_ENABLED`.
* ADR-039 (LLM Compliance Shadow second opinion) — X.1 primitive
  has shipped (observe-only); X.2 → X.3 promotion gated on the
  live 1-week soak + Compliance ratification workshop. See §20
  for the audit-bearing surface that the primitive emits today.
* ADR-041 (case-type axis + workspace consolidation) — **Accepted
  2026-05-13.** `case_type` + `email_classification` on OrderCase;
  `sap_block_code` on ExceptionRecord; `/exceptions` UI route
  retired in favour of single `/cases` workspace; automated Azure
  deploy from CI with health-check rollback. See §19.1 (audit-
  bearing fields) and ADR-041 for the full record.

---

## 20. L2 LLM Compliance Shadow — Observe-Only Audit Surface (ADR-039 X.1)

The Phase X.1 primitive ships an observe-only L2 second opinion behind the deterministic Compliance Shadow. The shadow runs after the L1 verdict, never blocks execution, and is asymmetric — only `DISAGREE_DOWNGRADE` is treated as a policy signal (the shipped path applies a `combine_verdicts` rule, see ADR-039 §4.5). Three audit-bearing surfaces exist today.

### 20.1 `ComplianceDecision.llm_shadow_verdict` and the persisted trace

Every `run_graph()` whose gating triggered L2 populates `ComplianceDecision.llm_shadow_verdict` (`contracts/models.py::ShadowLLMVerdict`). The trace persisted by `_persist_exception` (`api/routes/exceptions.py`) carries:

| Field | Source | Audit Use |
|---|---|---|
| `llm_shadow_verdict_action` | `state.shadow.llm_shadow_verdict.action` | `AGREE` / `DISAGREE_DOWNGRADE` / `ABSTAIN` — the field the override handler reads back to drive the X.2 → X.3 SLI |
| `shadow_policy_hits` | `state.shadow.policy_hits` | Concatenates L1 rule names + L2 hits prefixed with `LLM_SHADOW:` (per `compliance.shadow_llm.combine_verdicts`) — preserved into the case-level `aggregated_policy_hits` (Phase 28.5.x §28.5) so an auditor can drill from a case → matching trace |

Read with: `GET /api/v1/exceptions/{id}/trace`.

### 20.2 Per-tenant cache isolation (ADR-039 §5.5)

L2 invocations are cached at L4 per tenant. The cache key includes `tenant_id` so a cross-tenant cache hit is impossible (lock + Compliance ratification gate §4.1). Cache hits emit `shadow_llm_cache_hits_total` to the SLI surface §20.3 below; they do NOT re-emit a Langfuse trace.

### 20.3 Operational SLI surface — `/api/v1/metrics`

The asoe-core API serves a Prometheus text-format exposition at `/api/v1/metrics` (no auth — payload carries no tenant data; service is internal-cluster only per the ServiceMonitor at `k8s/core/observability/servicemonitor.yaml`). The §7.3 SLI families are emitted from `compliance.shadow_llm.ShadowLLMMetrics` via `api/metrics.py`:

| Family | Type | Audit Use |
|---|---|---|
| `shadow_llm_invocations_total` + `shadow_llm_invocations_by_trigger{trigger}` | counters | Total L2 calls + breakdown by gating reason |
| `shadow_llm_verdicts_total{action}` | counter | Verdict distribution (AGREE / DISAGREE_DOWNGRADE / ABSTAIN) |
| `shadow_llm_cache_hits_total` + `shadow_llm_cache_hit_rate` | counter + gauge | Cache effectiveness |
| `shadow_llm_timeouts_total`, `shadow_llm_unavailable_total`, `shadow_llm_validation_errors_total` | counters | Failure-mode telemetry |
| `shadow_llm_disagreement_rate`, `shadow_llm_abstain_rate` | gauges | X.1 → X.2 promotion gate; target bands per ADR-039 §6 |
| `shadow_llm_avg_latency_ms`, `shadow_llm_cost_usd_total` | gauges | Cost / latency observability; cost is recorded once at the call site and re-projected to Langfuse so the two never drift |
| `shadow_llm_reviewer_overrides_of_downgrade_total` | counter | **Phase 28.6** — increments on `/disposition` OVERRIDE + `/override/cosign` apply when `llm_shadow_verdict_action == "DISAGREE_DOWNGRADE"`. Wired in `api/routes/exceptions.py::_record_reviewer_override_of_llm_downgrade` |
| `shadow_llm_reviewer_override_rate_on_downgrades` | gauge | **Phase 28.6** — derived ratio; X.2 → X.3 ratification gate target ≤ 0.35 |
| `asoe_cases_returned_p99` (Phase 28.5.x §D7) | gauge | Rolling-window /api/v1/cases response size; the SLI re-opens cursor pagination when sustained ≥ 150 |

The Grafana dashboard JSON at `ops/observability/grafana/dashboards/shadow_llm.json` plots all of the above. Every panel carries a Langfuse trace-search deep link so an on-call operator drills from an aggregate spike to individual traces in one click.

### 20.4 Audit trust boundaries

* The SLI counters are operational telemetry — they are **not** the source of audit truth. The hash-chain audit log (`§10`, `policy_audit_log`) remains the SOX evidence-of-control surface.
* Reviewer override is recorded twice: once in the audit log (under `EXCEPTION_RESOLVED` / `EXCEPTION_OVERRIDE_COSIGNED` per §18.3) and once as the §20.3 counter increment. The audit row is load-bearing; the counter is a derived view operations watches for drift.
* `llm_shadow_verdict_action` persistence on `trace_data` is forward-only — overrides on records whose trace pre-dates the field do not contribute to the §20.3 reviewer-override counter (best-effort drift detection only).

### 20.5 Pending Compliance ratification before X.2 promotion

Per ADR-039 §6 the X.2 flip requires:

* 1-week observe-only X.1 soak with the Azure provider.
* Compliance ratification of §4.1 (combination rule) — pre-read at `docs/workshops/2026-05-09-deferred-items-virtual-workshop.md`, minutes pending.
* `knowledge/shadow_llm/metadata.yaml::rollout.financial_impact_threshold_usd` flipped from `null` to `10000` via SIGHUP per `docs/runbooks/shadow_llm_x2_rollback.md` §3.1.A.

Until those clear, X.2/X.3/X.4 are gated.
