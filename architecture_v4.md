# Architecture Spec: CPG Agentic AI Exception Management System (V4 — Verdict-Closed Core)

**Document Owner:** Principal AI Systems Architect
**Domain:** Consumer Packaged Goods (CPG) Supply Chain (Order-to-Cash)
**Scope:** V1.0 is strictly constrained to **Pricing & Promotional Exceptions**.
**Date:** 2026-05-01
**Design Reference:** [DESIGN.md](DESIGN.md) — maps these patterns to concrete modules, classes, and wiring.

**Lineage:** This document supersedes `architecture_v3.md` (dated
2026-04-26). v3 was the "Unified Core Specification" that consolidated
v2 with the V1-planned ASOE Core items from `consol_arch.md`. Since
v3 was finalised, the project shipped: ADR-025 (gateway reads moved
before shadow_audit), the Verdict 2026-04-22 three-pillar audit-
evidence governance, the `compliance/audit_bearing_registry.yaml`
mechanism with grandfather-clause discipline, the V005 migration
(intent CHECK constraint dropped), the V004 single-bag
`enrichment_context` persistence, env-driven JWT TTLs with bicep
parameterisation, real classifier confidence persistence (retiring
the legacy `80 if intent_selected else 0` synthesis), and the
LLM/deterministic cross-check disagreement routing. v4 absorbs
these into the spec so a reader does not need to mentally apply 7
ADRs to v3 to reconstruct current state.

**Two ADRs are Proposed but not yet shipped** — ADR-026 (event-
driven ingestion via Azure Event Hubs, Phase B) and ADR-027
(pipeline visualization hybrid timeline + DAG, currently rev. 3).
v4 references them as Proposed; their architectural content will
be absorbed into v4.1 once they ship + are ratified by the review
board chain (AI/LangGraph → Compliance → Tools Admin → Frontend
Platform → Compliance veto holder).

**How to read this document.** v4 is a delta-synthesis. Sections
that have changed since v3 carry the full updated content.
Sections that have not changed carry a one-line pointer back to
v3 (`See architecture_v3.md §X.Y — unchanged`). The result is a
single source of truth for current architectural state without
duplicating ~100 KB of stable foundational content.

---

## 1. Abstract Solution Architecture

See `architecture_v3.md` §1 — unchanged. Skill-Recipe Sandwich,
Compliance Shadow, deterministic state machine. The four core
innovations and the platform principles (Determinism Over
Autonomy; Compliance Before Execution; Decoupled Reasoning and
Execution; Observability as a First-Class Product) are
authoritative as written in v3.

**v4 addendum — fifth platform principle (Verdict 2026-04-22):**

5. **Audit-Evidence Completeness Is a Compliance Veto Surface.**
   The operator is consuming a SOX-relevant evidence payload to
   authorise a financially-binding decision. Partial-truth states
   (`"—"`, `"N/A"`, fabricated mid-range defaults, blended
   client-side fallback chains) are *not* acceptable visual
   shorthand — they are an audit defect Compliance holds veto
   over. Every audit-bearing field has one of three legal states
   only: present (render normally), structurally omitted
   (contextual field absent — render nothing), or "Context Not
   Required for Resolution" (conditional field whose predicate
   doesn't hold — render the labelled placeholder). This
   principle is enforced by `compliance/audit_bearing_registry.yaml`
   on the backend, the `build_analysis` graph node which routes
   coverage gaps to `TerminalStatus.AUDIT_CONTEXT_MISSING`, and
   the `<EvidenceBlock>` UI primitive on the frontend. See §11
   (Audit-Evidence Governance) for the full mechanism.

---

## 2. V1 Scope & Non-Functional Requirements

See `architecture_v3.md` §2 — unchanged. V1 scope (pricing &
promotional exceptions), the supported intents enumeration, the
shadow-verdict semantics, and the throughput / latency / RPO
targets are authoritative as written in v3.

**v4 amendment — supported intents.** v3 §2 lists 11 intents.
The same 11 ship in v4. The DB-layer `chk_exceptions_intent`
CHECK constraint that pinned this enum at the persistence
boundary is **retired** by V005 (`db/migrations/V005__drop_intent_check.sql`).
Intent vocabulary now lives exclusively in
`contracts/models.py::Intent`; adding a new intent requires zero
DB migration coordination. The Pydantic `Literal` and the
runtime `Intent` enum remain authoritative.

---

## 3. Skills, Recipes, and the Brain-Muscle Split

See `architecture_v3.md` §3 — unchanged. Skill semantics, recipe
semantics, the Brain↔Muscle decoupling, and the SkillLoader
contract are authoritative as written in v3.

---

## 4. Constrained Generation & Provider-Agnostic LLM Tier

See `architecture_v3.md` §4 — base content unchanged.

**v4 amendment — LLM/deterministic cross-check (Phase 25).**
When the active backend is *not* `DeterministicFallbackBackend`
(i.e. an LLM-backed classifier is live), `orchestration/nodes.py::classify`
runs the deterministic classifier in parallel and asks
`constraints/cross_check.py::cross_check` for a verdict:

```text
agreed       → use the LLM IntentDecision; state.confidence = LLM confidence
not agreed   → state.intent = deterministic intent
                 state.confidence = deterministic confidence
                 state.final_status = TerminalStatus.MANUAL_REVIEW_REQUIRED
                 last LLMCallTrace stamped cross_check_disagreement = True
```

This is the conservative shakeout posture per CLAUDE.md §5
(`MANUAL_REVIEW_REQUIRED` is a valid terminal state) and the
compliance-approved policy. The deterministic branch is cheap
(if/elif over the event payload, no network); the LLM call cost
is the same as without cross-check; we just refuse to act on the
LLM result when it diverges from the rule path. The disagreement
itself is a high-signal "don't auto-execute" trigger, recorded
on `LLMCallTrace.cross_check_disagreement` and forwarded to
LangFuse on the optional telemetry path.

Halt routing on disagreement is implicit, not via a graph edge —
classify itself sets `final_status`, and the next conditional
gate (`route_after_gate`) reads `final_status is not None` and
routes to `build_analysis` → END. shadow_audit, execute_recipe,
and apply_effects are skipped for disagreement-halted records.
This is the case ADR-027 calls out as the implicit gate the DAG
view will need to render specially.

---

## 5. Orchestration: The LangGraph State Machine (post-ADR-025)

This section **replaces** `architecture_v3.md` §5 graph-topology
content. ADR-025 reordered the graph so gateway READS land before
`shadow_audit`; v3 §5 documented the pre-ADR-025 ordering.

### 5.1 Topology

```
ingest → classify → load_skill → validate_circuit_breaker
  ├─[breach]→ build_analysis → END (FAIL_TO_HUMAN)
  └─[ok]→ select_recipe
       ├─[no recipe]→ build_analysis → END (FAIL_TO_HUMAN)
       └─[ok]→ resolve_dependencies         ← gateway READS happen here, BEFORE shadow
            ├─[required-gw fail]→ build_analysis → END (FAIL_TO_HUMAN)
            └─[ok]→ validate_types
                 ├─[invocation fail]→ build_analysis → END
                 └─[ok]→ shadow_audit
                      ├─[RED]→ build_analysis → END (BLOCKED, with audit evidence)
                      ├─[YELLOW]→ build_analysis → END (MANUAL_REVIEW_REQUIRED, with audit evidence)
                      └─[GREEN]→ execute_recipe → apply_effects → build_analysis → END
                                  (explain mode: explain_only → build_analysis → END)
```

Every terminal edge converges on `build_analysis` (the Pillar 2
composer node — see §11). The conditional-edge router
`route_after_gate` returns `"terminal"` when any prior node has
set `state.final_status`, `"continue"` otherwise.

### 5.2 Why gateway READS were moved before shadow

ADR-025 rationale, in one paragraph: gateway READS are evidence
acquisition, not execution. The Verdict 2026-04-22 / Pillar 1
commitment is that **every record carries audit-bearing
evidence regardless of compliance verdict** — a RED-shadowed
record needs the same enrichment_context as a GREEN one to
defend the decision under audit. Pre-ADR-025, gateway READS only
ran on the GREEN path, leaving RED/YELLOW records with empty
`enrichment_context` and routing them to
`AUDIT_CONTEXT_MISSING`. The reorder lifts gateway READS into
the proposal phase (after `select_recipe` / `resolve_dependencies`,
before `shadow_audit`), so audit-bearing evidence is captured
for every record. Shadow still gates *recipe execution and
effects* — that is its load-bearing job, per CLAUDE.md
Guardrail #4 — but evidence acquisition runs unconditionally.

### 5.3 build_analysis as the sole composer

`build_analysis` (`orchestration/nodes.py`) is the **sole
assembler** of the analysis payload the UI consumes. It calls
`api/analysis_composer.py` to project the recipe + enrichment
output into the typed `OrderAnalysis` contract, enforcing the
audit-bearing registry (§11) en route. Recipes return dicts;
the composer projects; UI sections are dumb projectors. Composition
logic does not live in recipes, in nodes between shadow and
execute, or in UI hooks. (Verdict 2026-04-22 / Guardrail #6 —
see §11 for the full statement.)

### 5.4 Halt-point semantics & the implicit classify-time gate

`route_after_gate` collapses every conditional into
`{terminal, continue}` based on `state.final_status`. The
**verdict that drove the route** is recorded as state
side-effects, not on the edge:
- `state.shadow.status` for shadow gates
- `state.explanation` + `LLMCallTrace.cross_check_disagreement`
  for the classify-time disagreement halt
- ad-hoc `final_status` reasons for circuit-breaker / no-recipe /
  required-gw / invocation-fail halts

This shape is correct for orchestration but **lossy for
visualization** — a single FAILED lifecycle today doesn't carry
the actual halt node or verdict. ADR-027 (Proposed, see §13)
addresses this with a verdict-vocabulary registration alongside
each `add_conditional_edges` call, plus an `ExecutedNode` trace
extension. v4 documents the current shape; v4.1 will absorb
ADR-027's resolution.

---

## 6. Persistence & Storage

This section **replaces** `architecture_v3.md` §6 storage-schema
content where it diverges; the rest is unchanged.

### 6.1 Schema migrations applicable to v4

| Migration | Applies to | Effect |
|---|---|---|
| V001 | All stores | Initial `exceptions` table + indices |
| V002 | All stores | Promote `original_event` and `reanalysis_history` to dedicated columns (was JSONB blob) |
| V003 | All stores | Hash-chained append-only audit log (Phase 20) |
| V004 | All stores | `enrichment_context JSONB NOT NULL DEFAULT '{}'` — single-bag persistence (Phase 23 / Pillar 1) |
| **V005** | All stores | **Drop `chk_exceptions_intent` CHECK constraint.** Intent vocabulary now lives in `contracts/models.py::Intent` only; adding a new intent requires no DB migration coordination. Adopted post-Verdict workshop (2026-04-30). |

### 6.2 `enrichment_context` — Pillar 1 single bag

`GraphState.enrichment_context: Dict[str, Any]` is the
authoritative bag for everything a recipe / gateway / composer
needs at audit time. Persisted via `V004__enrichment_context.sql`.
The legacy `state.resolved_data` field is retired; the
in-memory bridge with `resolved_data → enrichment_context`
fallback was removed when `enrichment_context` became the only
read path. Single source of truth for evidence; `audit_bearing_registry.yaml`
references field paths inside this bag.

### 6.3 `reanalysis_history` (current shape)

Today: `List[Dict[str, Any]]` carrying `prior_*` /  `new_*`
scalar snapshots (`shadow_verdict`, `final_status`,
`lifecycle_state`, `trace_id`) plus `attempt`, `attempted_at`,
`attempted_by`, `reason`. **No per-attempt executed-path
evidence** — this is a known gap that ADR-027 rev. 3 closes by
typing `ReanalysisHistoryEntry` and adding
`executed_nodes: list[ExecutedNode]` per attempt. v4.1 will
absorb the typed shape.

### 6.4 Confidence persistence

`trace_data["intent_confidence"]` carries `state.confidence`
(0.0-1.0 float; the IntentDecision's confidence) at every
`/resolve` and `/reanalyze` write site
(`api/routes/exceptions.py` ~L229 and ~L1245). The
`/api/v1/exceptions/{id}/analysis` read path scales it to a 0-100
int with `max(0, min(100, ...))` clamp; missing / zero / negative
values fall back to 0. Never a fabricated default. Closes the
deployed-system "every record at 80%" partial-truth state. Locked
by `tests/test_analysis_confidence_persistence.py` (4 regression
tests). See AUDITOR_GUIDE §2.1 for the audit-bearing statement.

---

## 7. API Contracts

See `architecture_v3.md` §7 — base content unchanged for the REST
endpoint surface, error envelope, idempotency, cursor pagination.

**v4 amendments:**

- `AnalysisResponse.confidence` (0-100 int) is sourced from the
  real classifier value; see §6.4 above.
- `AnalysisResponse` and `OrderAnalysis` carry the rich
  `*AnalysisData` enrichment fields per Verdict 2026-04-22 / Pillar 2.
  See §11 for the registry mechanism.
- `TraceResponse` carries `audit_context_missing_class` and
  `audit_context_missing_fields` when coverage failed — surfaced by
  `build_analysis` and rendered in the UI's DiagnosticsSection.

---

## 8. Auth, RBAC & Security

See `architecture_v3.md` §8 — base content unchanged.

**v4 amendments:**

### 8.1 Env-driven JWT TTLs

Access and refresh token lifetimes are now operator-tunable via
env vars:

| Variable | Default (sandbox) | Default (production) | Bicep param |
|---|---|---|---|
| `ASOE_ACCESS_TOKEN_TTL_SECONDS` | `86400` (24h) | `3600` (60min) | `accessTokenTtlSeconds` |
| `ASOE_REFRESH_TOKEN_TTL_SECONDS` | `2592000` (30d) | `604800` (7d) | `refreshTokenTtlSeconds` |

Resolved by `api/deps.py::_resolve_token_ttls` — pure function,
defensive against empty / malformed / zero / negative input
(falls back to per-`ASOE_ENV` default rather than crashing).
Operator-friendly presets documented in `infra/main.bicep`:
`900` (15 min), `3600` (1h), `86400` (24h). No process restart
required for the read; the bicep template wires the values as
container env vars.

### 8.2 Cross-check disagreement is a halt, not a bypass

The classify-time LLM/deterministic disagreement (§4 v4
amendment) routes to `MANUAL_REVIEW_REQUIRED` — it is *not* a
silent fallback. The disagreement is recorded on the
`LLMCallTrace` and forwarded to LangFuse so the cross-check
outcome is auditable. CLAUDE.md §5 ("Explicit failure is correct
behaviour") covers this — `MANUAL_REVIEW_REQUIRED` is a valid
terminal state, not a bug.

---

## 9. Observability & Tracing

See `architecture_v3.md` §9 — base content unchanged.

**v4 amendments:**

- Every `LLMCallTrace` carries `cross_check_disagreement`,
  `cross_check_llm_intent`, `cross_check_deterministic_intent`,
  `fallback_to_deterministic`, `fallback_reason` so the audit
  trail records every divergence between the LLM and the
  deterministic path.
- `pipeline_progress` WebSocket events (per `api/events.py::WSEvent`
  factory) are defined but **not yet emitted** by the orchestrator
  — the per-node emission gap is documented in ADR-026 §Phase B.2
  and resolved structurally by ADR-027 Phase B (when shipped).
  v4 documents the gap; v4.1 will absorb ADR-027's resolution.
- The architectural lock test
  `tests/architectural/exceptions_api_live_branches.test.ts` (UI
  side) walks every `LIVE_METHODS` entry asserting that
  `if (USE_REAL_API)` and the matching path fragment exist.
  Reanalyze-specific regression: the gate must appear textually
  before `MOCK_EXCEPTIONS.find` so a refactor cannot silently
  mock-ify the live path again.

---

## 10. Hardening & Failure Modes

See `architecture_v3.md` §10 — unchanged. Kill switch, explain
mode, circuit breaker, budget hard-block, and the
`FAIL_TO_HUMAN` / `MANUAL_REVIEW_REQUIRED` / `BLOCKED` /
`REJECTED` terminal-status taxonomy are authoritative as written.

---

## 11. Audit-Evidence Governance (Verdict 2026-04-22 — NEW in v4)

This section is **new in v4**. v3 has no concept of the three-pillar
governance, the `audit_bearing_registry.yaml` mechanism, or the
partial-truth veto.

### 11.1 The Verdict — what it commits to

The Verdict from the 2026-04-22 compliance workshop is explicit:
the rich `*AnalysisData` classes in `asoe-ui/src/types/exceptions.ts`
and their Pydantic mirrors in `api/schemas.py` are the **evidence
payload** a human operator consumes to authorise a SOX-relevant,
financially-binding decision. The product is committed to keeping
those types rich (do not prune to match current recipe output).
If a field is declared audit-bearing in
`compliance/audit_bearing_registry.yaml` but no recipe / gateway /
policy currently produces it, the correct response is to:

1. Add a gateway or extend the recipe's captured context so
   `state.enrichment_context` carries the missing evidence
   (Pillar 1 enrichment), or
2. Flag the gap in `compliance/audit_bearing_registry.yaml`
   under `grandfather_clauses` with a compliance-approved
   deadline.

**Never** silently remove a field from a `*AnalysisData` class or
`OrderAnalysis` to make coverage green — that is the partial-truth
state Compliance holds veto over.

### 11.2 The three pillars

| Pillar | Where | What it enforces |
|---|---|---|
| **1 — Enrichment persistence** | `GraphState.enrichment_context` (V004 JSONB column) | Every record carries audit-bearing evidence regardless of compliance verdict. Gateway READS run before shadow_audit (ADR-025) so RED/YELLOW records have the same evidence as GREEN ones. |
| **2 — Registry-aware composer** | `api/analysis_composer.py` + `compliance/audit_bearing_registry.yaml` | `build_analysis` is the sole assembler of the analysis payload. The composer projects `enrichment_context` + recipe output into the typed `*AnalysisData` contracts, checking each audit-bearing field against the registry. Coverage gaps → `TerminalStatus.AUDIT_CONTEXT_MISSING` with structured trace fields. Recipes do not assemble UI payloads; orchestration nodes between shadow and execute do not assemble UI payloads. |
| **3 — Dumb projector UI** | `<EvidenceBlock>` (`asoe-ui/src/components/ui/EvidenceBlock.tsx`) | UI sections render `analysis.foo` as given. Three legal presence states only: present (render normally), structurally omitted (contextual field absent — render nothing), or "Context Not Required for Resolution" (conditional field whose predicate doesn't hold — render the labelled placeholder). Ad-hoc `"—"` / `"N/A"` / `data.field ?? fallback` patterns are code-review anti-patterns. |

### 11.3 The audit-bearing registry

`compliance/audit_bearing_registry.yaml` declares, per
`*AnalysisData` class:
- which fields are audit-bearing (must be present for coverage
  to pass)
- which fields are conditional (predicate-gated; see Pillar 3
  legal-state taxonomy)
- which fields are structurally omitted in some contexts
- grandfather clauses with compliance-approved deadlines

Adding a new audit-bearing field is a CODEOWNERS-gated change
on the registry. Removing one is the partial-truth veto
surface — Compliance holds the gate.

The registry was extended in Phase 25.6 with an `LLMProvenance`
section carrying 3 audit-bearing rows (`llm_provider_used`,
`llm_model_id`, `llm_request_id`); `pending_signoff: true` until
the workshop follow-up flips to false. Summary tally went from
107 → 110 audit-bearing rows (82 → 85 ratified).

### 11.4 Coverage outcomes

| Outcome | When | What it means |
|---|---|---|
| **COMPLETE** | Recipe ran, all audit-bearing fields populated, registry coverage check passes | Operator sees the full evidence payload; the record can be presented for resolution |
| **AUDIT_CONTEXT_MISSING** | Coverage check fails | The composer routes to this terminal state. The trace carries `audit_context_missing_class` + `audit_context_missing_fields` so an operator and an auditor know exactly which field is missing and from which class. The UI renders the structured gap (DiagnosticsSection); the record cannot be presented for resolution. |
| **BLOCKED / MANUAL_REVIEW_REQUIRED** | Shadow RED / YELLOW | Same evidence payload as COMPLETE (Pillar 1 ensured pre-shadow gateway READS), surfaced for human review. |

### 11.5 Grandfather-clause discipline

When the Verdict workshop closed (2026-04-22), four grandfather
clauses were active: `price_analysis_gateway_gap` (T4),
`delivery_delay_financial_gap` / `overmax_gateway_gap` /
`moq_gateway_gap` (T5). All four have been retired; gateway
READS for the previously-grandfathered fields land via SAP doc /
contract / block-status / customer-master / promotion / SLA
contract gateways. Real SAP integration is a separate platform
track; current production is StubGateway-backed. **Backend-backed
status: 10 of 10 enrichment sections** (`price_hold_analysis`,
`edi_mismatch_analysis`, `delivery_delay_analysis`,
`overmax_analysis`, `moq_analysis`, `pallet_analysis`,
`duplicate_detection`, `order_comparison`, `backorder_analysis`,
`price_analysis`).

---

## 12. UI Architecture (cross-reference)

The UI architecture is authoritative in `asoe-ui/ui_architecture.md`.
v4 highlights only the cross-cutting commitments that affect
backend contracts:

- UI sections are dumb projectors (Pillar 3). No frontend
  composition of enrichment payloads. The backend's
  `build_analysis` is the sole assembler.
- Confidence pills, halt-point banners, pipeline-step labels,
  and verdict displays are sourced **from the backend** — never
  fabricated. The UI must distinguish present / structurally-
  omitted / "context not required" via `<EvidenceBlock>` rather
  than ad-hoc fallback chains.
- WebSocket polling fallback (Section 8.4) ALIGNED 2026-05-01;
  Container Apps closes idle WS at 4 minutes, so after 5
  consecutive failed reconnects the hook switches to interval
  polling on `/api/v1/exceptions/{id}`.
- FAILED-state render branch ALIGNED 2026-05-01: the detail panel
  renders "Pipeline failed at <node>" instead of conflating
  FAILED with shadow-pending. Topology-side fix tracked by
  ADR-027 (see §13).

---

## 13. Proposed (deferred to v4.1)

These ADRs are in *Proposed* status; their architectural content is
**not** absorbed into v4. v4.1 absorbs them once shipped + ratified.

### ADR-026 — Event-driven ingestion via Azure Event Hubs (Phase B)

`docs/adr/ADR-026-event-driven-ingestion.md`. Adds a push-async
ingestion path (Event Hubs Kafka surface + per-source connectors
+ bus consumer) that lands canonical `OrderEvent`s into the same
internal handler `POST /api/v1/exceptions/resolve` calls.
Distinguishes ingestion (push, async, bus) from enrichment
(pull, sync, gateway) — the user's question that conflated these
is explicitly resolved in the ADR. Phase B.2 documents per-node
real WaterfallStepper timings as deferred (orchestrator emission
gap — `WSEvent.pipeline_progress` factory exists but is uncalled).

### ADR-027 — Pipeline visualization hybrid (rev. 3)

`docs/adr/ADR-027-pipeline-visualization-hybrid.md`. Replaces
`WaterfallStepper` with two trace-derived surfaces: an
operator-first `EventsTimeline` (renders only the nodes that
ran, with halt point + reason emphasised) and an audit-first
`PipelineDAG` (full topology with the taken path highlighted
and verdict labels on each conditional edge). Topology comes
from a new `GET /api/v1/pipeline/topology` endpoint that
introspects `compiled_graph.get_graph()`. The trace is extended
with `executed_nodes: list[ExecutedNode]` carrying per-node
timing, decision, exit verdict, policy hits, and gateway
sub-spans. `ReanalysisHistoryEntry` becomes typed and carries
`executed_nodes` per attempt so reanalysis does not destroy
prior-path audit evidence. Reviewer chain: AI/LangGraph →
Compliance → Tools Admin → Frontend Platform → Compliance veto
holder. Estimated 8-9 days end-to-end.

### Why deferred

ADRs are *decision records*, not *specifications*. Including
them in v4 before they ship would repeat the v3 failure mode
(writing aspirations into the spec). v4.1 absorbs each ADR
within one release of its shipping; v4 is the stable
re-baseline of what is *done* as of 2026-05-01.

---

## 14. Document Lineage & Future Versioning

**v2 → v3:** v3 unified the v2 core engine spec with the V1-planned
ASOE Core items from `consol_arch.md` (the platform-wide
architecture in `asoe-ui`). v2 was marked superseded.

**v3 → v4:** v4 absorbs ADR-025, the Verdict 2026-04-22 three-pillar
governance, the `audit_bearing_registry.yaml` mechanism, V004 +
V005 migrations, env-driven JWT TTLs, the LLM/deterministic
cross-check, and real classifier confidence persistence. v3 is
marked superseded by v4 — but its foundational sections (§1, §2,
§3, §4 base, §7 base, §8 base, §9 base, §10) remain authoritative
prose unless v4 explicitly amends them. A reader using v4 is
expected to read v3 §X for unchanged sections and v4 §X for
changes; v4 carries explicit pointers (`See architecture_v3.md
§X.Y — unchanged`) for those cases.

**v4 → v4.1 (planned):** absorbs ADR-026 (event-driven ingestion)
and ADR-027 (pipeline visualization hybrid) once shipped +
ratified. The §13 "Proposed" pointers retire; the §11 audit-
governance section gains the `executed_nodes` evidence shape;
§12 UI cross-reference points at the new EventsTimeline + DAG
surfaces.

**Versioning discipline going forward.** A new architecture
version ships when:
- A cross-cutting governance principle changes (Verdict
  2026-04-22 was such a moment — Compliance gained a veto
  surface that touches recipes, the composer, the UI, and
  the registry).
- The graph topology changes structurally (ADR-025 added a
  new layer of audit-evidence acquisition; ADR-027 changes
  how the pipeline is rendered).
- The persistence model changes (V004 single-bag, V005 enum
  decoupling, future event-driven ingest landing tables).
- Three or more ADRs accumulate that touch the same surface
  area (the v3 → v4 trigger).

Patch revisions (v4.1, v4.2) are appropriate when a single
ADR ships and is absorbed; major versions are appropriate when
the synthesis of those ADRs reveals a structural shift (e.g.
the move from "evidence is for GREEN paths only" to "every
record carries evidence regardless of verdict" was a structural
shift, not a patch).
