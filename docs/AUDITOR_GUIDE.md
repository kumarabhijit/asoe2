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
| **Allowed values** | `CONTRACTUAL_CORRECTION`, `CREDIT_BLOCK`, `MASS_PRICING_ERROR`, `DUPLICATE_PO`, `PRICE_HOLD_RELEASE`, `EDI_MISMATCH` |
| **Enforcement** | `AllowedIntent` Pydantic `Literal` type; Pydantic raises `ValidationError` on any other value |
| **Routing fork** | `EDI_850_LINE_MISMATCH` events whose `metadata.mismatch_sub_type == "PRICE_MISMATCH"` are classified as `CONTRACTUAL_CORRECTION` (not `EDI_MISMATCH`), preserving `PriceAdjustmentRecipe.py` as the single source of truth for pricing. Enforced in `constraints/fallback_backend.py:classify_intent` and mirrored in `skills/loader.py:select_for_event`. |
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
| **Allowed values** | `PriceAdjustmentRecipe.py`, `CreditHoldReleaseRecipe.py`, `DuplicatePORecipe.py`, `PriceHoldReleaseRecipe.py`, `EdiMismatchRecipe.py` |
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
with spans for `classify`, `load_skill`, `shadow_audit`, and `execute_recipe`,
plus a `terminal_status` score that enables dashboard filtering and success-rate
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
| **Where** | `PATCH /exceptions/{id}/disposition` (OVERRIDE sub-type) and `POST /exceptions/{id}/override/cosign` |
| **Rule** | On an override, the caller's `user.sub` must not equal the record's prior `resolved_by`. On a cosign, the caller's `user.sub` must not equal `resolution_data.pending_override.initiator`. |
| **Principals exempt** | Prior resolvers whose subject starts with `system:` are exempt — the control targets human self-approval, not agent auto-resolutions that humans must still be able to correct. |
| **Failure mode** | `403 SOD_VIOLATION` with a message naming the boundary violated. Idempotency-Key lookups run **before** the SoD check so a retry of a successful first call still returns the cached success. |

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
