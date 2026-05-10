# Architecture Spec: CPG Agentic AI Exception Management System (V5 — Case-Centric, Five-Layer Architecture)

**Status:** Proposed (gated on ADR-038 §6 + §8.5 + ADR-039 §4.1 + §6 ratification)
**Document Owner:** Principal AI Systems Architect
**Domain:** Consumer Packaged Goods (CPG) Supply Chain (Order-to-Cash)
**Scope:** Same V1.0 scope as v4 — Pricing & Promotional Exceptions, plus Manual Order Intake (ADR-034 / ADR-038 §3.2) and the case-centric primitives ADR-038 introduces.
**Date:** 2026-05-09
**Design Reference:** [DESIGN.md](DESIGN.md) — concrete module / class / wiring map.

**Lineage:** v5 supersedes `architecture_v4.md` (dated 2026-05-01). v4 was the stable re-baseline of what was done as of 2026-05-01. Between then and 2026-05-09 the project shipped:

* **ADR-038 (case-centric order intake)** — the five-layer architecture (L0 Knowledge → L4 Harness), the `OrderCase` parent entity, lazy materialisation, tier-graduation, compaction, SLA stamping, the Case Agent loop + 18-tool surface, the V012 `case_events` replay log + V013 `case_locks` cross-pod mutex, the `/api/v1/cases/*` read surface, the asoe-ui `/cases` primary CSR surface, and the `EMAIL_ORDER_ENTRY → MANUAL_ORDER_INTAKE` channel-neutral cleanup.
* **ADR-039 (L2 LLM Compliance Shadow second opinion)** — the X.1 observe-only primitive, the `compliance/shadow_llm.py` constrained-output L2 Shadow, the per-tenant cache, SLI counters, the `combine_verdicts` truth table that flips on at X.2+ ratification, Azure OpenAI / Anthropic / local Ollama provider seams.
* **ADR-040 (case-level four-eyes cosign)** — extends the existing ADR-029-derived per-exception cosign control to operate on the case lifecycle, behind `ASOE_CASE_COSIGN_ENABLED`. Ships with the X.0 code path; X.1 ratification = config flip.

v4's two Proposed ADRs (ADR-026 event-driven ingestion, ADR-027 pipeline visualization) remain Proposed and are not absorbed here — their architectural content lands in v5.1 once ratified.

**How to read this document.** v5 is a delta-synthesis. Sections that have changed since v4 carry the full updated content. Sections that have not changed point back to v4 (`See architecture_v4.md §X.Y — unchanged`). v3 / v4 stable foundational content is not duplicated.

---

## 1. The Five-Layer Architecture (NEW; supersedes v4 §1)

ADR-038 §4 introduces the five-layer model. This is the single most important architectural change since v4. Every subsequent section references the layer by number.

### 1.1 Layer responsibilities

| Layer  | Responsibility                                                                  | Authoritative artefacts                                                                                                       |
|--------|---------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------|
| **L0** Knowledge | Skill bundles, anchor / on-demand examples, server-rendered assets, specs. | `knowledge/skills/<name>/`, `knowledge/shadow_llm/`, `knowledge/compaction/`, `knowledge/policy/sla_per_customer_tier.yaml`. |
| **L1** Deterministic primitives | Recipes, the deterministic Compliance Shadow, gateway operations, validators. | `recipes/`, `compliance/shadow.py`, `gateways/`, `constraints/`.                                                              |
| **L2** Bounded LLM primitives | Single-call LLM primitives with constrained output: intent classifier, recipe-selector, attachment extractor, **L2 Shadow second opinion**. | `compliance/shadow_llm.py`, `agents/primitives/extract_attachment.py` + `extract_providers.py`, `constraints/llm_backend.py`. |
| **L3** Case Agent | The bounded `while`-loop with the 18-tool surface, working-memory loader, budget enforcer. Operates on a specific `OrderCase`. | `agents/case_agent.py`, `agents/case_tools.py`, `agents/working_memory.py`, `agents/budget.py`.                               |
| **L4** Harness | The cross-cutting concerns: per-case mutex, tool-call interception → replay log, tier graduation, compaction trigger, L2 Shadow invocation, SLA monitor. | `agents/harness.py`, `agents/compaction.py`, `agents/sla.py`, `agents/backfill.py`.                                           |

### 1.2 Routing — what reaches which layer

```
inbound event
    │
    ├── EMAIL_ORDER_ENTRY_REQUEST + ASOE_CASE_AGENT_ENABLED=1 ──▶ L4 harness ──▶ L3 agent ──▶ L1 / L2 tools
    │
    └── otherwise ─────────────────────────────────────────────▶ deterministic LangGraph (v3 §4 / v4 §3 unchanged)
                                                                    │
                                                                    └── shadow_audit calls L2 LLM Shadow (observe-only at X.1; truth-table-active at X.2+)
```

The case-agent path and the deterministic graph are **mutually exclusive per event** — one or the other runs end-to-end. The graph never reaches into the agent loop and vice versa. Their points of contact are `OrderCase` (case open/attach is shared) and the L2 Shadow (both paths invoke it).

### 1.3 Why the layered model

* **CODEOWNERS map** (ADR-038 §8.5). Each layer has a different review chain. L0 changes (knowledge bundles) need Compliance + domain SME. L2 (LLM primitives) need Engineering + Compliance + Tools Admin. L3 (agent loop) needs Engineering + Compliance veto. L4 (harness) needs Engineering + SRE.
* **Determinism boundary.** L0 + L1 are deterministic by construction. L2 is bounded but non-deterministic (LLM calls). L3 is the only layer with an open loop. L4 is deterministic again (cross-cutting infrastructure). The boundary lets auditors reason about which decisions are reproducible.
* **Replayability.** L4's `case_events` replay log captures every (tool_call, tool_result) pair the agent emitted. Same inputs + same L0 bundle version + same L2 model id + temperature 0 → same trace.

---

## 2. The OrderCase entity (NEW; v4 had no equivalent)

`contracts/models.py::OrderCase` is the parent entity for all events on a single business order. ADR-038 §6.

### 2.1 Lifecycle (7 states)

```
OPEN_AGENT_PROCESSING ── agent step ──▶ {OPEN_AWAITING_HUMAN, OPEN_AWAITING_BUYER,
                                          OPEN_AWAITING_ERP, RESOLVED, FAILED, BLOCKED}
```

Distinct from the per-exception `LIFECYCLE_STATES` (architecture_v3.md §9.1) — the case lifecycle tracks "what's blocking forward progress on this business order"; the exception lifecycle tracks "where this individual record is". A case is RESOLVED when every child is terminal-closed.

### 2.2 Materialisation policy (forward-only tier graduation)

| Source            | T1 (stateless)             | T2 (stateful)                                | T3 (stateful + compacted)                         |
|-------------------|----------------------------|----------------------------------------------|---------------------------------------------------|
| Manual Order      | n/a — opens at T2 eagerly  | First email arrival                          | First non-clean event triggers compaction trigger |
| Automated Order   | Clean events stay T1       | First non-clean event lazily materialises    | First non-clean event triggers compaction trigger |

`agents/harness.py::graduate_tier_if_needed` enforces the forward-only invariant: T1 → T2 → T3, never demote. T3 cases run with the compacted working-memory summary (`agents/compaction.py::apply_compaction_if_needed`).

### 2.3 Correlation keys (lookup-or-create on inbound events)

Four key types resolve an inbound event to an existing case via `case_store.find_by_correlation`:

1. `sales_order_id` (highest priority — ERP-emitted)
2. `customer_po_number`
3. `edi_transaction_id`
4. `source_email_id`

First match wins. New keys discovered after open join the case via `register_correlation`.

### 2.4 Persistence

* `db/migrations/V009__order_case.sql` — `order_case` parent table.
* `db/migrations/V010__case_correlation_keys.sql` — `(tenant_id, key_type, key_value) → case_id` index.
* `db/migrations/V011__backfill_orphan_cases.sql` — Postgres-side companion to `agents/backfill.py` for legacy `ExceptionRecord` rows.
* `db/migrations/V012__case_events.sql` — append-only replay log (V5 NEW).
* `db/migrations/V013__case_locks.sql` — cross-pod mutex via UNIQUE-conflict (V5 NEW).

In-memory and Database-backed implementations match surface-for-surface (`api/store.py::CaseStore`, `api/store.py::DatabaseBackedStore`, `db/repository.py::CaseEventRepository` / `CaseLockRepository`, `agents/harness.py::DatabaseBackedToolCallReplayLog` / `DatabaseBackedCaseLockManager`). `_select_replay_log()` / `_select_lock_manager()` factories pick the DB-backed adapter when `DATABASE_URL` is set.

---

## 3. The Case Agent and the L4 Harness (NEW)

### 3.1 Inner-loop semantics (`agents/case_agent.py::run_case_agent`)

```
while True:
    if budget.is_exhausted(): return BUDGET_EXHAUSTED
    frame = build_working_memory(case, skill, current_event, last_actions, registry)
    response = llm_provider.call(frame)             # L2 LLM call — single
    budget.deduct(input=..., output=..., cost=...)
    for call in response.tool_calls:
        result = invoke_tool(registry, ctx, call)   # L1 / L2 primitives
        actions.append({tool, status, summary})
        if call.tool_name in HALT_TOOLS:
            return {RESOLVED, ESCALATED, AWAITING_BUYER, AWAITING_ERP}
```

The single non-deterministic step is `llm_provider.call(frame)`. Tools are L1 / L2 primitives. The loop is bounded by `agents.budget.CaseBudget.for_case(case)` per the §8.1 limits (T1 4k/1k/1iter; T2 16k/4k/6iter; T3 8k/2k/8iter).

### 3.2 Outer harness (`agents/harness.py::run_agent_step`)

The harness wraps the inner loop with:

1. **Per-case mutex** — `CaseLockManager.try_acquire(tenant_id, case_id)` returns `None` when held; the caller bails out instead of running concurrent steps. Cross-pod via V013 `case_locks` (UNIQUE PK on `case_id`). In-process via `threading.RLock`.
2. **Tier graduation** — `graduate_tier_if_needed` fires forward-only T2 → T3 on the first non-clean event.
3. **Tool-call interception** — every `(tool_call, tool_result)` pair the agent emits is also persisted to the replay log (V012 `case_events` table). The agent does not write here — the harness does, so audit replay reads from a single canonical source.
4. **Compaction trigger** — `agents.compaction.apply_compaction_if_needed` evaluates the per-event-type templates (V5 NEW: `agent_step` / `tool_call` / `shadow_decision` / `override` / `escalation` / `case_open` / `sla_breach` / `compaction.template.md`) against the §7.4 binding triggers (8k tokens / 25 events / 7 days).
5. **L2 Shadow observe-only invocation** — at X.1 the verdict is recorded, never moves status. At X.2+ ratification (config flip, no code change), the truth table from §4 below activates.

### 3.3 Routing predicate

`agents/harness.py::should_route_to_case_agent(event, enabled)` — restricts the routable set to `EMAIL_ORDER_ENTRY_REQUEST` (V5 NEW: tied to the `MANUAL_ORDER_INTAKE` intent). Default off via `ASOE_CASE_AGENT_ENABLED`. Live deployments cut over via env-only change.

---

## 4. The L2 LLM Compliance Shadow (ADR-039)

### 4.1 Architecture

The L2 LLM Shadow runs **after** the L1 deterministic Shadow per ADR-039 §3.1. It produces a constrained-output verdict from a closed three-action vocabulary:

```python
class ShadowLLMVerdict(BaseModel):
    action: Literal["AGREE", "DISAGREE_DOWNGRADE", "ABSTAIN"]
    reason: str = Field(min_length=1, max_length=200)
    confidence: float = Field(ge=0.0, le=1.0)
    policy_concerns: list[str] = []        # closed L0 vocabulary
    bundle_version: str = ""
    model_id: str = ""
    request_id: Optional[str] = None
    cache_hit: bool = False
    latency_ms: int = 0
    cost_usd_estimate: float = 0.0
```

The schema deliberately omits any `DISAGREE_UPGRADE` action — **asymmetric authority is structural**, not a post-hoc filter (ADR-039 §3.2).

### 4.2 Provider seams (V5 NEW)

* **Stub** — `compliance/shadow_llm.py::StubLLMShadowProvider` (default; tests never hit the network).
* **Azure OpenAI** — `compliance/shadow_llm_azure.py::AzureOpenAIShadowProvider`. Uses the SDK's `response_format={"type": "json_schema"}` to enforce the verdict schema server-side. Selected when `AZURE_OPENAI_SHADOW_DEPLOYMENT` is set.
* **Anthropic / local Ollama** — Protocol-compatible; not yet wired (procurement decision was Azure).

### 4.3 X.2+ combiner (V5 NEW; flag-gated)

`compliance/shadow_llm.py::combine_verdicts` encodes the §4.1 truth table:

| L1     | L2                        | Final              | Lifecycle              |
|--------|---------------------------|--------------------|------------------------|
| GREEN  | AGREE / ABSTAIN           | GREEN              | proceed                |
| GREEN  | DISAGREE_DOWNGRADE + ≥thr | YELLOW             | MANUAL_REVIEW_REQUIRED |
| GREEN  | DISAGREE_DOWNGRADE + <thr | GREEN              | proceed                |
| YELLOW | (any)                     | YELLOW             | MANUAL_REVIEW_REQUIRED |
| RED    | (NEVER INVOKED)           | RED                | BLOCKED                |

The `thr` (`bundle.financial_impact_threshold_usd`) is `None` at X.1 (observe-only — verdict never moves status). X.2 ratification = flip to `10000` in `knowledge/shadow_llm/metadata.yaml::rollout`. X.3 = flip to `500`. No code change at flip time.

### 4.4 Tenant-isolated cache

`ShadowLLMCache` keys on SHA-256 of `(tenant_id, bundle_version, model_id, canonical(request))`. Tenant inclusion is mandatory per ADR-038 §5.8 / ADR-039 §5.5 — never serve a Tenant-A entry to a Tenant-B request even when other inputs match. 24-hour TTL.

### 4.5 SLI surface

`compliance/shadow_llm.py::ShadowLLMMetrics` — module-level counters mirroring ADR-039 §7.3:

* `invocations_total`, `invocations_by_trigger`, `cache_hits_total`
* `verdicts_by_action` (AGREE / DISAGREE_DOWNGRADE / ABSTAIN distribution)
* `timeouts_total`, `unavailable_total`, `validation_errors_total`
* `latency_ms_sum`, `latency_ms_count` (rolling p99 alerting)
* `cost_usd_total`
* `skipped_red_total`, `skipped_below_floor_total`

Helper methods (`disagreement_rate()`, `abstain_rate()`, `cache_hit_rate()`, `avg_latency_ms()`) drive the SLI dashboard.

---

## 5. The case-level four-eyes / cosign control (ADR-040, V5 NEW)

ADR-040 extends the existing per-exception cosign flow (architecture_v4.md §4) to operate on the case lifecycle:

* `OrderCase.pending_override: Optional[CasePendingOverride]` — typed envelope carrying initiator, action, reason tag, aggregate financial impact, child_exception_ids, notes.
* `POST /api/v1/cases/{id}/override` (X.0; flag-gated `ASOE_CASE_COSIGN_ENABLED`) — initiates a case-level override; transitions case status → `OPEN_AWAITING_HUMAN`.
* `POST /api/v1/cases/{id}/override/cosign` (X.0; flag-gated) — second-reviewer decision. Approve → `RESOLVED`; reject → `OPEN_AGENT_PROCESSING`.
* SoD invariants identical to the per-exception flow — initiator ≠ cosigner, manager+ role, mandatory notes.
* Audit events: `CASE_OVERRIDE_INITIATED` / `_COSIGNED` / `_REJECTED`.

The exception-level flow is preserved unchanged. This is additive.

---

## 6. The L0 Knowledge Layer (NEW; v4 had no equivalent)

### 6.1 Skill bundles

Every skill lives as a bundle under `knowledge/skills/<name>/`:

```
knowledge/skills/<name>/
  SKILL.md                   # human + agent-readable reasoning guide
  metadata.yaml              # schema + intents + token budgets + runtime_includes allowlist
  examples/                  # earned anchor / on-demand examples
  assets/                    # server-rendered template bodies (never enter agent context)
  specs/                     # Compliance-reviewed product spec (humans navigate; agent never sees)
```

10 bundles ship: `back-order-resolution`, `delivery-delay`, `duplicate-po`, `edi-mismatch`, **`manual-order-intake`** (V5 NEW name; was `email-order-entry` per ADR-038 §3.2 channel-neutral cleanup), `moq-round-up`, `over-max-trim`, `pallet-alignment`, `price-hold-release`, `pricing-reconciliation`.

### 6.2 The shadow_llm bundle (V5 NEW)

`knowledge/shadow_llm/`:

```
knowledge/shadow_llm/
  system_prompt.md             # the L2 Shadow's policy guidance
  concerns_vocabulary.yaml     # closed list of named policy concerns
  metadata.yaml                # bundle version, rollout config, inference params
  few_shot_examples/           # earned by X.1 disagreement traces
  anchor_examples/             # the first 5–10 land via Compliance review
```

Compliance + Engineering CODEOWNERS gate every change. The X.1 anchor-example accrual mechanism (`scripts/earn_anchor_examples.py`, V5 NEW) walks production disagreement traces and emits candidate examples for Compliance review.

### 6.3 Compaction templates (V5 NEW)

Per-event-type compaction templates under `knowledge/compaction/`:

* `agent_step.template.md` — agent loop iteration
* `tool_call.template.md` — direct (tool_call, tool_result) pair
* `shadow_decision.template.md` — Compliance Shadow verdict (L1 OR L2)
* `override.template.md` — human-triggered override
* `escalation.template.md` — case escalation
* `case_open.template.md` — case materialisation event
* `sla_breach.template.md` — SLA monitor emission
* `compaction.template.md` — recursive (compaction event itself)
* `__general__.template.md` — fallback when no per-event-type match

YAML frontmatter declares `audit_keys` per event type; markdown narrative documents the per-event-type signal for Compliance reviewers.

### 6.4 SLA policy

`knowledge/policy/sla_per_customer_tier.yaml` — Strategic 4h / Mid-Market 24h / Long-tail 72h / default 48h. Loaded at agent / harness startup via `agents/sla.py`.

---

## 7. UI surface changes (asoe-ui; V5 NEW)

* **`/cases`** is the primary CSR work surface (ADR-038 §H.6). The page wires NavBar with `activeTab="cases"`, consumes `useAuth` + `useHealth`, renders the SLA-sorted case list.
* `/inbox` and `/exceptions` retain as **filtered case-list views** of `/cases`. The X.0 reframe ships `CaseViewBanner` on each page (info-blue strip linking through to `/cases?source=manual_order` for inbox; no filter for exceptions). The full data-hook swap (rows projected from `casesApi.list` rather than `INBOX` mock / `exceptionsApi.list`) is V5.1.
* `casesApi.list/get` in `src/lib/api.ts` calls `/api/v1/cases` directly when `NEXT_PUBLIC_USE_REAL_API=1`.
* RBAC: `cases` tab gated by `exceptions:read` — same permission as the existing inbox / exceptions tabs (case detail panel renders the same audit-bearing evidence).

---

## 8. Sections unchanged from v4

* §1 (Abstract Solution Architecture, four core innovations, platform principles) — see `architecture_v4.md` §1.
* §2 (Data lineage, audit-bearing registry) — see `architecture_v4.md` §2.
* §3 (LangGraph state machine for the deterministic-graph path) — see `architecture_v4.md` §3.
* §4 (Per-exception four-eyes cosign control) — see `architecture_v4.md` §4. ADR-040 is additive; per-exception flow is preserved unchanged.
* §5 (Compliance Shadow deterministic primitive) — see `architecture_v4.md` §5. v5 adds the L2 LLM second opinion alongside, not in place of.
* §6 (Recipe registry + invocation discipline) — see `architecture_v4.md` §6.
* §7 (Gateway READS before shadow per ADR-025) — see `architecture_v4.md` §7. The gateway-evidence path is unchanged on either the deterministic-graph or the case-agent path.
* §8 (Persistence) — see `architecture_v4.md` §8. v5 adds `order_case`, `case_correlation_keys`, `case_events`, `case_locks` tables.
* §9 (Observability) — see `architecture_v4.md` §9. v5 adds the ADR-039 §7.3 SLI surface.
* §10 (Security / RBAC) — see `architecture_v4.md` §10. v5 adds the `cases` tab to `compute_visible_tabs` (gated on `exceptions:read`).
* §11 (Tenant isolation) — see `architecture_v4.md` §11. v5 reinforces — every L2 cache key includes `tenant_id` per ADR-038 §5.8 / ADR-039 §5.5.
* §12 (Architectural lock tests) — see `architecture_v4.md` §12. v5 adds `tests/test_routes_cases.py`, `tests/test_harness.py`, `tests/test_shadow_llm*.py`, `tests/test_routes_cases_cosign.py`.

---

## 9. Versioning discipline

Per the v4 §14 rule a new architecture version ships when:

> *— A cross-cutting governance principle changes…*
> *— The graph topology changes structurally…*
> *— The persistence model changes…*
> *— Three or more ADRs accumulate that touch the same surface area.*

ADR-038 + ADR-039 + ADR-040 between them trigger all four conditions:

* **Governance:** L0 Knowledge Layer + the per-layer CODEOWNERS map (ADR-038 §8.5).
* **Graph topology:** the L3 / L4 case-agent path is structurally distinct from the deterministic graph (§1.2 routing).
* **Persistence:** `OrderCase` + correlation keys + event log + lock table.
* **Cumulative ADRs:** ADR-034 (in-flight) + ADR-038 + ADR-039 + ADR-040 all touch the same surface area.

v5 status remains **Proposed** until:

1. Compliance ratifies ADR-038 §6 (L0 governance) + §8.5 (CODEOWNERS map).
2. Compliance ratifies ADR-039 §4.1 (combination rule) + §6 (phased rollout X.2+).
3. Compliance ratifies ADR-040 §2 + §2.2 (case-cosign truth table + SoD).

Once ratified, v5 moves to **Accepted** and v4 is marked superseded. The X.2 / X.3 / X.4 / case-cosign-on flips are then ConfigMap edits, no code redeploy.

---

## 10. Definition of Done

v5 is **Accepted** when:

* Reviewer chain has signed off: AI/Agentic Engineering Architect → Compliance Veto Holder → Tools Admin / SRE → Domain SME → Product Owner.
* The three Compliance ratifications above have happened.
* The flip-flags (`ASOE_CASE_AGENT_ENABLED`, `AZURE_OPENAI_SHADOW_DEPLOYMENT`, `ASOE_OCR_PRIMARY=azure_di`, `ASOE_CASE_COSIGN_ENABLED`, `knowledge/shadow_llm/metadata.yaml::financial_impact_threshold_usd`) are deployable per-environment via the existing Kustomize overlays.
* The acceptance test suite (`tests/test_*` for every primitive landed in 2026-05-09 round) is green and pinned in CI.

---

*End of architecture_v5.md (Proposed).*
