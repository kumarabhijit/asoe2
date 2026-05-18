# ARCHITECTURE.md
**Platform:** ASOE (Agentic Supply-Chain Operations Engine)
**Version:** 1.0.0
**Repositories:** asoe2 (Core Backend), asoe-ui (Frontend Control Tower)
**Status:** Unified Authoritative Reference
## 1. Executive Summary
The Agentic Supply-Chain Operations Engine (ASOE) is an enterprise-grade orchestration layer designed to sit atop legacy Order Management Systems (OMS) and Enterprise Resource Planning (ERP) systems. Its primary objective is the deterministic, compliance-first resolution of Order-to-Cash (O2C) exceptions (e.g., pricing discrepancies, credit holds, duplicate purchase orders) in the Consumer Packaged Goods (CPG) sector.
**Platform Principles:**
 * **System-First, Human-in-the-Loop:** The UI is not a dashboard; it is a control tower. The system acts autonomously up to the limit of its confidence and compliance thresholds, prompting humans only for high-leverage approvals.
 * **Decoupled Reasoning & Execution:** AI is restricted to the "Skill" layer (classification and reasoning). Execution is strictly delegated to immutable, code-bound "Recipes."
 * **Verifiable Determinism:** LLM outputs are constrained, and every action is audited by a deterministic Policy Shadow before execution.
**V1 Scope:** Focuses primarily on Pricing & Promotional Exceptions with a defined 11-node execution pipeline and 8-state exception lifecycle.
## 2. System Context
ASOE operates as a highly secure bridge and cognitive layer between upstream signal generators and downstream systems of record.
 * **Upstream (OMS/B2B Portals):** Systems generating the raw order telemetry and identifying initial deviations.
 * **ASOE Core:** Ingests the deviation, classifies the intent, applies enterprise policy thresholds, selects a recovery SOP, and stages the execution.
 * **Downstream (ERP - SAP/Oracle):** The system of record where ASOE executes the deterministic write-back (e.g., applying a discount code, releasing a hold) via strictly typed API adapters.
The boundary is explicitly drawn: ASOE does not replace the ERP; it acts as an intelligent, automated operator interacting with the ERP's existing interfaces.
## 3. Platform Architecture Overview
The platform utilizes a containerized, decoupled architecture optimized for Kubernetes deployment, structured into five primary runtime domains:
 1. **ASOE UI:** The Next.js 14 agent-first frontend.
 2. **ASOE Core (Workflow Runner):** The LangGraph-powered state machine and FastAPI gateway.
 3. **Inference Server:** vLLM cluster utilizing PagedAttention for low-latency reasoning on complex supply chain manifests.
 4. **State & Vector Store:** PostgreSQL (with pgvector extension installed for V2 readiness) managing all transactional and trace data.
 5. **Event Bus & Cache:** Redis cluster handling WebSocket pub/sub, rate limiting, and ephemeral state caching.
## 4. Component Topology
### Development vs. Production
 * **Local Sandbox (Dev):** Uses Docker Compose. Inference is mocked or routed to a lightweight local model. Redis and Postgres run in lightweight containers.
 * **Production Fortress (AKS):** Deployed on Azure Kubernetes Service. Multi-tenant isolation via Kubernetes namespace per tenant and PostgreSQL Row-Level Security (RLS) policies (see §9 for the two-layer isolation contract). Inference runs on dedicated GPU node pools.
## 5. ASOE Core Integration: Skill-Shadow-Recipe
The asoe2 core is built around an 11-node LangGraph pipeline. The central tension—AI flexibility vs. enterprise determinism—is resolved via the **Skill-Shadow-Recipe** pattern.
### 5.1 The 11-Node LangGraph Pipeline (orchestration/nodes.py)
 1. ingest: Standardizes the incoming OrderEvent payload.
 2. classify: Uses constrained generation to map the event to a known exception category.
 3. load_skill: Retrieves the non-executable reasoning context for the agent.
 4. circuit_breaker: Hard-coded thresholds that immediately abort if catastrophic conditions are met.
 5. shadow_audit: Evaluates proposed actions against contracts/policy.py. Returns GREEN, YELLOW, or RED. On a YELLOW verdict, the node calls `interrupt()` to suspend the graph pending human review (see §5.4).
 6. select_recipe: Maps the approved action to an immutable recipe ID.
 7. validate_types: Pydantic validation of all parameters required for the recipe.
 8. resolve_dependencies: Fetches required external data via the Hexagonal Gateway Layer.
 9. execute_recipe: Runs the deterministic Python function.
 10. apply_effects: Commits the transaction to the external ERP via service account context (see §9).
 11. END: Terminal state, finalizing the TraceRecord.
### 5.2 GraphState Schema (contracts/models.py)
State is passed through the graph via a strictly typed 16-field envelope. Key fields include:
 * trace_id (UUID): Propagated end-to-end.
 * tenant_id (UUID): Ensures multi-tenant isolation.
 * exception_type (Enum): E.g., PRICING_MISMATCH.
 * shadow_verdict (Enum): GREEN (auto-execute), YELLOW (requires HITL), RED (halt).
 * recipe_id (String): The selected deterministic SOP.
 * policy_overrides (Dict): Injected thresholds specific to the tenant.
### 5.3 Constrained Generation & Hardening
 * **3-Tier Backend Chain:** Primary LLM call → Outlines (JSON schema enforcement) → Deterministic Fallback (hardcoded rule if the LLM fails).
 * **Workflow Runner:** Implements the Saga Pattern with LIFO (Last-In-First-Out) compensation. If execute_recipe fails halfway, preceding steps are undone in reverse order.
 * **Hardening:** KILL_SWITCH and EXPLAIN_MODE are environment variables allowing instant halting or read-only trace generation without container restarts.

### 5.4 HITL Pause & Resume Protocol
When the `shadow_audit` node returns a YELLOW verdict, the graph suspends immediately via LangGraph's `interrupt()` mechanism before advancing to `select_recipe`. The full `GraphState` is checkpointed to PostgreSQL via LangGraph's `PostgresSaver` checkpointer, keyed by `trace_id`. The exception transitions to the `PENDING_APPROVAL` state and the UI is notified over WebSocket.

**Resume path:** `POST /v1/exceptions/{id}/approve` is the exclusive resume entry point. The endpoint:
 1. Validates the caller's JWT holds OPERATOR or ADMIN role (RBAC enforced here, not inside the graph — see §9).
 2. Rehydrates the `GraphState` from the PostgreSQL checkpoint.
 3. Calls `graph.invoke(None, config)` to resume execution from the interrupt point, advancing to `select_recipe`.

**Rejection path:** `POST /v1/exceptions/{id}/reject` transitions the exception to FAILED with reason `HITL_REJECTED` without resuming the graph.

**Timeout policy:** If no approval or rejection is received within the configured HITL window (default: 48 hours; configurable per tenant via `policy_overrides.hitl_timeout_hours`), the Workflow Runner's background scheduler marks the exception FAILED with reason `HITL_TIMEOUT` and routes to the `MANUAL_REVIEW` recipe. The checkpoint is retained for audit purposes.

**GREEN path:** On a GREEN verdict, `interrupt()` is not called; the graph advances directly to `select_recipe` with no checkpoint or human interaction required.

## 6. API Contract
Full REST + WebSocket endpoint specification.
### 6.1 REST Endpoints (Subset of 13 Core Routes)
| Method | Route | Description |
|---|---|---|
| POST | /v1/exceptions/ingest | Gateway for OMS to push new OrderEvent payloads. Requires service account OPERATOR token. |
| GET | /v1/exceptions | Paginated list of exceptions (filterable by status/tenant). |
| GET | /v1/exceptions/{id} | Detailed exception state, including active GraphState. |
| POST | /v1/exceptions/{id}/approve | HITL intervention: resumes a YELLOW-verdict graph. Requires caller JWT with OPERATOR or ADMIN role. |
| POST | /v1/exceptions/{id}/reject | HITL intervention: terminates a YELLOW-verdict graph with HITL_REJECTED. Requires OPERATOR or ADMIN role. |
| GET | /v1/traces/{trace_id} | End-to-end LangGraph trace retrieval for audit. |
| PUT | /v1/policies/{tenant_id} | Update policy_overrides for a specific tenant. Requires ADMIN role. Every invocation is immutably audit-logged (see §9). |
### 6.2 Standard Error Envelope
```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "Recipe parameter 'discount_pct' exceeds policy threshold.",
    "trace_id": "req-123e4567-e89b-12d3-a456-426614174000",
    "details": {"provided": 15, "max_allowed": 10}
  }
}

```
## 7. Data Architecture
Persistence is handled by PostgreSQL, utilizing a tenant-isolated schema design.
### 7.1 Exception Lifecycle (8 States)
> **Authoritative detail:** this section is an abstracted summary. For the code-accurate model — the 12-state `LIFECYCLE_STATES`, the `final_status` / Shadow-verdict / `OrderCase.status` surfaces, and the deterministic maps between them — see `docs/STATUS_MODEL.md`.

The lifecycle follows the order of pipeline execution. The `shadow_audit` node (AUDITING) runs before any human decision point; its verdict determines whether the PENDING_APPROVAL state is entered.

 1. DETECTED → 2. ANALYZING → 3. AUDITING (Shadow Audit) → 4. PENDING_APPROVAL (HITL Wait State, YELLOW path only) → 5. EXECUTING → 6. RESOLVED (Terminal Success) → 7. FAILED (Terminal Error) → 8. ARCHIVED

**Path variants by shadow_verdict:**
 * **GREEN:** DETECTED → ANALYZING → AUDITING → EXECUTING → RESOLVED (PENDING_APPROVAL is skipped entirely)
 * **YELLOW:** DETECTED → ANALYZING → AUDITING → PENDING_APPROVAL → EXECUTING → RESOLVED
 * **RED:** DETECTED → ANALYZING → AUDITING → FAILED (PENDING_APPROVAL and EXECUTING are never entered)

### 7.2 Database Schema Highlights
 * **Exceptions Table:** Stores current state, tenant_id, and recipe_id.
 * **TraceRecord Table:** Stores the complete LangGraph step-by-step history linked via trace_id.
 * **Checkpoints Table:** LangGraph `PostgresSaver` checkpoint store, keyed by trace_id. Retains serialized `GraphState` for paused (PENDING_APPROVAL) exceptions and post-resolution audit. Entries are archived alongside their exception record.
 * **pgvector Deferral:** The vector extension is installed, and the exception_history table contains a context_embedding column. However, active indexing and similarity searches are deferred to V2.
## 8. Real-Time Communication Protocol
WebSockets backed by Redis pub/sub are used to push per-node LangGraph execution updates to the asoe-ui.
### 8.1 WebSocket Envelope
```json
{
  "event_type": "graph_node_complete",
  "timestamp": "2026-04-08T18:18:00Z",
  "payload": {
    "trace_id": "123e4567-e89b-12d3-a456-426614174000",
    "node": "shadow_audit",
    "status": "SUCCESS",
    "output_diff": {"shadow_verdict": "YELLOW"}
  }
}

```
 * **Resilience:** The UI implements automatic reconnection with exponential backoff and falls back to HTTP polling of /v1/exceptions/{id} if WebSockets fail.
## 9. Security & Compliance
 * **Authentication:** Dual-flow supporting SSO (SAML/OIDC) for enterprise users and secure Email/Password for sandbox environments. Both flows issue JWTs that include an `env` claim (`production` | `sandbox`). The FastAPI dependency injector validates the `env` claim against the `ASOE_ENV` environment variable at every authenticated request boundary; a `sandbox` token presented to a production service returns 403 immediately, before any business logic executes. The two environments use separate IdP configurations and non-overlapping JWT signing keys so tokens cannot be cross-forged.
 * **Multi-Tenancy — Two-Layer Isolation:** Tenant data isolation is enforced at two independent layers to provide defense-in-depth:
   * **Application layer:** The FastAPI dependency injector extracts `tenant_id` from the JWT and injects it as a required parameter into every database query and Redis channel subscription. Invariant #5 (§14) enforces this at the code level; tests verify no query against the `exceptions` or `traces` tables omits the `tenant_id` predicate.
   * **Database layer:** PostgreSQL Row-Level Security (RLS) policies are active on the `exceptions`, `traces`, and `checkpoints` tables. The connection pool sets the `app.current_tenant_id` session variable before executing any query; the RLS policy `USING (tenant_id = current_setting('app.current_tenant_id')::uuid)` enforces the boundary at the database engine level. A bug that omits the application-layer filter will be blocked by the RLS policy and return an empty result set rather than cross-tenant data.
 * **RBAC — Autonomous (GREEN) Path:** When the graph executes autonomously, there is no in-flight human JWT. The Workflow Runner operates under a system service account holding a permanent OPERATOR-scoped service token. Authorization is enforced at the **ingest boundary** (`POST /v1/exceptions/ingest`) and at service-account provisioning time — not inside the graph. The `apply_effects` node operates under this established service-account context.
 * **RBAC — HITL (YELLOW) Path:** Authorization for human-initiated actions is enforced at the **approve and reject endpoints** (`POST /v1/exceptions/{id}/approve` and `/reject`). The FastAPI dependency verifies the caller's JWT holds OPERATOR or ADMIN role before the graph checkpoint is rehydrated. A stale or missing JWT at this boundary returns 403 and the exception remains in PENDING_APPROVAL.
 * **Policy Override Controls:** `PUT /v1/policies/{tenant_id}` is restricted to the ADMIN role. Every invocation writes an immutable record to the `policy_audit_log` table before the new value is applied, capturing: caller identity (JWT `sub`), timestamp, `tenant_id`, the full previous value, and the full new value. Policy changes take effect immediately for new exceptions; in-flight exceptions use the `policy_overrides` snapshot captured in their `GraphState` at ingest time and are unaffected.
 * **Trace Propagation:** The X-Trace-ID header is required at the API Gateway, injected into the LangGraph GraphState, logged via Python's standard logging, and persisted in PostgreSQL.
## 10. Continual Learning Architecture (V2 Scope)
ASOE maps LangChain's 3-layer learning model to its own architecture, safely constrained by the Shadow Audit:
 1. **Harness Agent (Evaluation):** An offline agent that parses TraceRecords ending in a RED shadow verdict to generate synthetic evaluation datasets for future testing.
 2. **Per-Tenant Context:** Utilizing the deferred pgvector implementation, ASOE will perform RAG on a tenant's historical RESOLVED exceptions to provide better context during the load_skill node.
 3. **Model Fine-Tuning:** Extracting validated "Skill-to-Recipe" mappings from the database to fine-tune the classification model, reducing latency and cost over time.
## 11. UI Architecture
The asoe-ui merges an Agent-First design system with standard utility libraries.
### 11.1 Component Strategy
 * **Standard Primitives (Shadcn/ui):** Used for non-agentic UI elements to ensure accessibility and speed (e.g., DataTable, Dialog, Select).
 * **Agent-First Components (Custom):** 12 specialized components tailored for cognitive observability.
   * AgentReasoningCard: Visualizes the LLM's thought process behind a load_skill output.
   * WaterfallStepper: Real-time tracking of the LangGraph state machine.
   * MetricTile: Executive summary of exception blast radius.
   * ActivityIndicator, Badge, ContextPanel, etc.
### 11.2 Design Tokens (design-tokens.css)
Strict adherence to 45+ CSS variables.
 * Brand constraint: --asoe-brand: #5A4BD6 is reserved exclusively for the primary CTA, navigation logo, and active tab to minimize cognitive overload.
 * Semantic states: --asoe-status-green, --asoe-status-yellow, --asoe-status-red map directly to the shadow_verdict.
## 12. Deployment Topology
### 12.1 Azure Architecture (AKS)
 * **Ingress:** Azure Application Gateway routing traffic to the UI and API.
 * **Compute:** Azure Kubernetes Service (AKS). UI and Core run on standard node pools. The vLLM Inference Server runs on dedicated NC-series GPU nodes.
 * **Data Services:** Azure Database for PostgreSQL (Flexible Server) and Azure Cache for Redis.
 * **Networking:** Strict VNet peering. The DB and Redis subnets are completely isolated from the public internet, accessible only via the AKS backend subnet.
## 13. Architecture Decision Log (ADRs)
 * **ADR-001 (Core Engine):** *Decision:* Use a deterministic state machine (LangGraph) over a pure LLM agent. *Rationale:* Pure LLMs cannot guarantee the compliance required for ERP write-backs.
 * **ADR-002 (UI Framework):** *Decision:* Hybrid Shadcn/ui + Custom Design System. *Rationale:* Speeds up boilerplate development while retaining tight control over complex, agent-specific visualizations.
 * **ADR-003 (Vector Search):** *Decision:* Install pgvector but defer active querying to V2. *Rationale:* Reduces V1 cognitive load and latency while laying the schema groundwork for future RAG features.
 * **ADR-004 (Real-time Protocol):** *Decision:* Per-node WebSocket events with a typed envelope. *Rationale:* Essential for the "Control Tower" experience, requiring the UI to reflect backend state in milliseconds.
 * **ADR-005 (HITL Resume):** *Decision:* Use LangGraph `interrupt()` with `PostgresSaver` for HITL pause/resume. *Rationale:* Checkpointing to PostgreSQL ensures the GraphState survives pod restarts and provides an auditable record of the exact state at the moment of human review. The resume path is a single, guarded API endpoint — not an internal graph signal — keeping RBAC enforcement at the system boundary.
 * **ADR-006 (Environment Isolation):** *Decision:* Embed an `env` claim in every JWT and validate it at the FastAPI dependency boundary. *Rationale:* Separate signing keys and an enforced claim check prevent sandbox credentials from being usable against production endpoints. This is cheaper and more auditable than network-level separation alone.
## 14. Appendix: Execution Invariants
These 11 rules are enforced at the compiler/framework level and validated by core tests:
 1. **Shadow-Mandatory:** No recipe_id can progress to execute_recipe without a preceding shadow_audit node execution yielding a non-RED verdict.
 2. **Stateless-Skills:** Skills are pure functions/prompts. They lack the network bindings to interact with the database or ERP gateway.
 3. **LIFO-Compensation:** Sagas must execute rollback functions in exact reverse order upon a failure in a multi-step recipe.
 4. **Trace-Continuity:** Every active graph node must extract and log the trace_id from the GraphState.
 5. **Tenant-Isolation:** tenant_id must be present in every SQL WHERE clause interacting with the exceptions or traces tables.
 6. **Immutable-Recipes:** Deployed recipes are version-locked. Logic changes require a new recipe version (e.g., PriceAdjustment_v2).
 7. **Deterministic-Fallback:** If the generation chain fails all retries, the graph must route to a hardcoded MANUAL_REVIEW recipe.
 8. **RBAC-Strict:** RBAC is enforced at system entry points, not inside graph nodes. For YELLOW-path exceptions, `POST /v1/exceptions/{id}/approve` validates the caller's JWT holds OPERATOR or ADMIN role before resuming the graph. For GREEN-path (autonomous) exceptions, authorization is enforced at the ingest boundary via service-account OPERATOR token. The `apply_effects` node operates under the established service-account context and does not parse or re-validate a human JWT.
 9. **Kill-Switch-Priority:** If ASOE_KILL_SWITCH=true, the circuit_breaker node immediately transitions the graph to END.
 10. **Explain-Mode-Integrity:** If ASOE_EXPLAIN_MODE=true, apply_effects acts as a mock, returning success without executing external API calls.
 11. **Type-Safety:** Output from validate_types must strictly satisfy the Pydantic model defined by the selected recipe_id.
