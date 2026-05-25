# ADR-042: Porting the AgenticOM "Customer Inbox" Prototype into the Case Architecture

**Status:** Proposed (2026-05-23)
**Date:** 2026-05-23
**Deciders:** Principal AI/Agentic Engineering Architect; Frontend Platform; Domain Modeller; Compliance Engineer; UX Architect; Product Owner.
**Applies to:**
* asoe2: `contracts/models.py::OrderCase` + `api/store.py::ExceptionRecord`, `api/schemas.py`, `api/routes/cases.py`, `api/routes/sandbox.py`, `api/analysis_composer.py` + `api/analysis_adapters.py`, `orchestration/nodes.py::build_analysis` + `orchestration/graph.py`, `agents/harness.py` + `agents/case_tools.py`, `recipes/`, `gateways/`, `constraints/`, `knowledge/`, `compliance/audit_bearing_registry.yaml`, `api/case_events.py` + `api/routes/ws.py`, `openapi/asoe2.openapi.json`.
* asoe-ui: `docs/customer-inbox-implementation-plan.md` (paired execution plan), `src/app/cases/*`, `src/lib/api.ts`, `src/types/*`, `src/hooks/*`.

**Related:**
* ADR-026 (event-driven ingestion — inbound channel → event).
* ADR-027 (pipeline-visualization hybrid — `/pipeline/topology` + per-record trace).
* ADR-034 (email-order-entry skill — extraction + autonomy vocabulary).
* ADR-038 (case-centric order intake — `OrderCase` parent + lazy materialisation).
* ADR-039 (LLM Compliance-Shadow second opinion).
* ADR-040 (case-level four-eyes cosign).
* ADR-041 (case-type axis + `/cases` workspace consolidation — **retired the standalone Customer Inbox**).
* asoe-ui `docs/prototype_gap_analysis.md` §4 (Customer Inbox Gaps), §9 (Priority Matrix). NOTE: that doc predates ADR-041 and still recommends "enhance `/inbox`"; this ADR supersedes that framing — see §4.

---

## Implementation status (2026-05-24)

The plan in this ADR has been **built and merged** (asoe2 PR #166 + asoe-ui PR #185),
but the **Status above remains *Proposed* by design** — see the blocker below.

Delivered against the §2.2 scorecard:
* **Phases 0–7** — complete. All nine prototype tabs ported as deterministic,
  data-presence-driven sections: AI Analysis / Entities / SAP Data (P2), Order
  Entry extraction + ERP-submit disposition with cosign>$10k (P3), Draft Reply +
  Simulate-Inbound backend injector + live WS events (P4), EDI 850 builder +
  viewer (P5), Change Analysis (recipe-homed, variable-cardinality) (P6),
  Knowledge Graph + DraftReply schema (P7). All 8 §2.2.1 section schemas are
  exported; `tests/test_inbox_gate_openapi_contract.py` is now a standing
  hard gate (xfail marker removed).
* **Phase 8 + productionization** — every strategy-§6 DoR gate implemented:
  sanitizer, autonomy vocab (display), #2 no-auto-execute, #3 SAP re-price
  cosign, #4 calibration, #5 delivery idempotency, #6 effect outbox + DB
  persistence (`effect_outbox`, V015) + reconciliation worker/scheduler, #7
  ingest→terminal SLO histogram, #8 gateway circuit breaker, #9 business/
  disposition audit hash-chain, #10 XSS/CSP + SSRF guard (wired into
  `gateways/attachment_fetch.py`), #11 automation-bias SLIs, S sandbox isolation.

Deferred — NOT built, by design:
* **Constraint Graph** — §2.1/§5b direct reuse of `get_pipeline_topology` +
  `/exceptions/{id}/trace`; the Change Analysis section already renders the
  constraint data. A dedicated SVG surface is duplicative; revisit on demand.
* **Real attachment fetcher** — the SSRF guard + `attachment_fetch` gateway +
  stub are wired and tested; a live fetcher waits on a production attachment store.

**Blocker keeping Status = Proposed:** the strategy doc (§8) ties acceptance to
the `autonomy_vocab_version` hard gate, which requires **autonomy-v2
dual-control compliance sign-off**. In this pre-prod project the human approval
*step* is waived for merge, but the *mechanism* stays intact — so the status is
not flipped to *Accepted* unilaterally. Flip to *Accepted* once that sign-off
lands (Phase 8 final item).

---

## 1. Context

The `AgenticOM_Prototype_NC.html` static prototype contains a rich
**Customer Inbox** screen: a shared email mailbox where inbound
customer emails are AI-classified, validated against SAP, and
actioned. Its detail panel exposes up to nine context-dependent
tabs — Email, AI Analysis, Entities, SAP Data, Order Entry, EDI 850
Audit, Change Analysis (10-constraint / 7-agent evaluation),
Constraint Graph, Knowledge Graph — plus a "Simulate Inbound" modal,
an "AI Intake Flow" pipeline sub-view, and an AI draft-reply
workflow.

The prototype is a **client-only demo**: it calls an LLM directly
from the browser, computes EDI 850 segments, financial deltas, and
constraint verdicts in client JavaScript, and stores everything in
`MB_EMAILS` mock arrays. None of that survives contact with the
ASOE production architecture, where:

* The UI is a **dumb projector** — business logic lives in recipes
  (`recipes/`), Compliance Shadow, and the single analysis composer
  (`api/analysis_composer.py`). (`CLAUDE.md` Guardrail #1; asoe-ui
  Guardrail #6.)
* Machine-consumed LLM output must be **constrained at generation
  time** (`CLAUDE.md` Guardrail #3).
* Execution is gated by **Compliance Shadow** (GREEN/YELLOW/RED) and,
  above threshold, **cosign** (ADR-040).
* Reads/writes are **tenant-scoped + RBAC-gated** (`api/routes/cases.py`).
* Evidence fields an operator relies on are **audit-bearing**
  (`compliance/audit_bearing_registry.yaml`) and may not be pruned
  (Guardrail #6/#7).

Crucially, **ADR-041 already retired the standalone Customer Inbox.**
`/inbox` permanently redirects to `/cases?source=manual_order`; the
`/cases` **two-pane** workspace (queue + detail, with the record
picker stacked at the top of the detail pane) is the single
canonical surface. (asoe-ui `662c9d2` collapsed the short-lived
3-column layout back to two panes; the dedicated `RecordListPane`
column was removed and the picker is now inline in the detail pane.)
ADR-041 also added the `case_type ∈ {EMAIL_ENTRY, BLOCK}` axis and
`email_classification ∈ {NEW_ORDER, ORDER_CHANGE, INQUIRY,
COMPLAINT, OTHER}` — i.e. the prototype's email-intent taxonomy
already exists in the domain model.

The Product Owner has asked for the prototype's Customer Inbox
**features** to be made real (dynamic, backend-driven) at
production-grade. The open question this ADR answers is **where each
prototype feature lives** given that the inbox screen itself is gone.

## 2. Decision

Re-express every Customer Inbox feature as **backend-authoritative
data surfaced through the existing `/cases` workspace** — an
`EMAIL_ENTRY` case lens — rather than reviving a parallel inbox app.
No prototype business logic is ported to the client; each panel
becomes a Pydantic-typed analysis section assembled by
`build_analysis` and rendered by a dumb UI projector.

### 2.1 Feature → architectural-home mapping

| Prototype feature | Backend home (asoe2) | Already exists? |
|---|---|---|
| Mailbox / email list | `OrderCase` where `case_type == "EMAIL_ENTRY"`; `GET /api/v1/cases?case_type=EMAIL_ENTRY` (filter to be added beside existing `source`/`status`/`intents`/`since`/`q`) | Case model ✅ (ADR-041); filter ⛔ |
| Email intent + confidence | Skill classification → `email_classification` + `OrderAnalysis.confidence`, constrained enum from `useHealth` | Partial (ADR-034) |
| Autonomy L1–L4 | `contracts/policy.py` autonomy vocabularies (single source of truth) | ✅ — **reconcile semantics, see §5** |
| AI Analysis / Entities / SAP Data tabs | `build_analysis` node + `api/analysis_composer.py` projecting gateway reads (ADR-025 gateway-before-shadow) | Composer ✅; new sections ⛔ |
| Order Entry (extract→form→validate→submit) | Extraction **gateway** (constrained LLM) + `EMAIL_ORDER_ENTRY` **recipe** + master-data **constraints** + ERP-submit recipe (Shadow-gated); corrections logged to audit | Skill ✅ (ADR-034); full path ⛔ |
| EDI 850 Audit | Deterministic server builder (port client `buildEDI850`) as a gateway/recipe; read endpoint | ⛔ |
| Change Analysis (constraints / scenarios / decision / financials) | **Recipe** evaluations (deterministic, thresholds from `contracts/policy.py`) + Compliance Shadow + composer. NOT `constraints/` (that is the constrained-LLM-generation router) and NOT `agents/harness.py` (a gated-off single-case sequential loop, not a parallel fan-out). Render **variable cardinality** (N constraints / M scenarios), not the prototype's fixed 10/7/3; agent timings are cosmetic, not audit-bearing. | recipe home ⛔ |
| Constraint Graph | Reuse `orchestration/graph.py::get_pipeline_topology` + per-record `/exceptions/{id}/trace` (ADR-027) — do **not** build a new surface | Topology ✅; per-case projection ⛔ |
| Knowledge Graph | **Net-new derived projection** over `OrderCase` / `ExceptionRecord` entities. **Correction:** the `knowledge/` package is NOT a graph producer — it is `compaction / policy / shadow_llm / skills` (the skill/policy knowledge base). No KG data source exists today; this is deferrable (see §3 / §5b). | ⛔ no source |
| AI Draft Reply (gen/edit/approve/send) | Reply-draft recipe action (`REQUEST_CLARIFICATION` / `REQUEST_BUYER_CONFIRMATION`, already in UI `ACTION_LABELS`) + email gateway send, Shadow + cosign | Action vocab ✅; send path ⛔ |
| Simulate Inbound | `api/routes/sandbox.py` injector (sandbox env only) → real pipeline | Sandbox harness ✅; scenario injector ⛔ |
| AI Intake Flow (6-step) | `GET /api/v1/pipeline/topology` + WS step events | ✅ (ADR-027) — UI wiring only |
| Token usage (`recordUsage`) | `api/metrics.py` from the server-side LLM gateway | ✅ — never client-side |

### 2.2 Backend workstream (sequence: skill → recipe → orchestration → API)

1. **Contracts & schemas.** Add the section models to
   `api/schemas.py` — `OrderEntryExtraction`, `Edi850Document`,
   `ConstraintEvaluation` (+ `ConstraintCheck`), `ScenarioOption`,
   `ChangeDecision`, `KnowledgeGraphPayload`, `DraftReply`. These are
   the SOX-relevant evidence contract; build them rich (Guardrail
   #6/#7) and register audit-bearing fields in
   `compliance/audit_bearing_registry.yaml`, using `grandfather_clauses`
   for fields no recipe populates yet. Each new `model_validator`
   gets a focused unit test **and** a paired asoe-ui mock-data lock.
2. **Gateways** (`gateways/`, ADR-025 reads-before-shadow):
   email-ingest normaliser; order-extraction gateway (constrained
   generation, Guidance/Outlines; usage metered to `api/metrics.py`);
   EDI-850 deterministic builder (pure, fully unit-testable);
   email-send gateway for replies.
3. **Recipes** (`recipes/`): target the **canonical
   `MANUAL_ORDER_INTAKE`** recipe path (the `EMAIL_ORDER_ENTRY`
   binding is a legacy alias / stub, sunset 2026-08-12 — Architect
   correction). Submit-to-ERP (BAPI / EDI 850), draft-reply action,
   constraint evaluation. Recipes return dicts; the composer
   projects them — no "ready-to-render" composition inside recipes
   (Guardrail #6).
4. **Constraint evaluation lives in recipes, not `constraints/`.**
   Panel correction (Architect): `constraints/` is the
   constrained-LLM-generation backend router (Guidance/Outlines),
   **not** a business-rule evaluator, and `agents/harness.py` is a
   per-`(tenant,case_id)`-mutexed single-case loop currently gated
   off (`should_route_to_case_agent → False`), **not** a 7-agent
   parallel fan-out. The prototype's checks (Inventory ATP,
   Production, Transport, Warehouse, Order Lifecycle, SLA,
   Financial, Dependencies, Network, Priority) are **deterministic
   recipe logic** reading thresholds from `contracts/policy.py`
   (financial >$10k = the ADR-040 cosign threshold). They emit a
   **variable-length** list the composer projects; the UI renders N
   constraints / M scenarios. Putting evaluation in the harness
   would violate Guardrail #1 (execution outside recipes) and
   depend on an unshipped loop.
5. **Orchestration & composer (composer-first; split only on an
   ADR-031 trigger).** The composer (`api/analysis_composer.py`)
   remains the **sole** assembler (Guardrail #6), projecting
   already-resolved `state.enrichment_context`. **Fidelity
   correction:** the default is the *unified* `OrderAnalysis`
   composer payload — sections are projected fields on it, **not**
   eagerly-invented per-section endpoints. Splitting a heavy section
   (EDI-850, knowledge graph, change-analysis) out to a dedicated
   read projection happens **only when a pre-committed ADR-031-style
   trigger fires** (P95 latency / payload-size budget), starting as
   a materialised view. (`/cases/{id}/records` and
   `/exceptions/{id}/trace` are the existing precedents for a
   separate read.) Ensure `orchestration/graph.py` topology covers
   the order-entry path.
6. **API routes — reuse the disposition surface, do NOT add
   bespoke `/cases` write verbs (fidelity correction).**
   `api/routes/cases.py` is **read-only by explicit design** ("any
   write path that wants to alter case state must go through the
   existing override / cosign / disposition flows"); the only case
   writes that exist are ADR-040 `override` + `override/cosign`. The
   canonical per-record action surface is on **exceptions**
   (`/exceptions/resolve`, `/exceptions/{id}/disposition`,
   `/escalate`, `/reanalyze`, `/challenge`, `/admin-release`,
   `/override/cosign`). Therefore:
   * **Reads / list:** `GET /cases?case_type=EMAIL_ENTRY` filter
     (list lens). Extraction and draft-generation are **reads /
     enrichment** (no lifecycle mutation) — surfaced via the
     analysis payload + a sandbox/agent trigger, **not** new write
     endpoints.
   * **Order-entry submit (ERP write)** → a **disposition /
     recipe-execution** through `/exceptions/{id}/disposition`
     (+ override/cosign >$10k, ADR-040), NOT a new
     `POST /cases/{id}/order-entry/submit`. It is financially
     binding and inherits the four-eyes gate.
   * **Operator corrections** → carried as disposition parameters;
     before/after + actor + timestamp land in the hash-chained
     audit log (ADR-023). "Logged for retraining" is a **separate,
     gated, consented, de-identified** export — never the same sink
     as the immutable audit capture.
   * **Reply send (outbound write)** → a **disposition action**
     (Shadow + cosign >$10k). Audit MUST persist `body_hash` +
     content + actor + verdict before send (analogous to
     `EmailSourceData.body_hash`). The email **body is human-facing**
     → constrained generation (Guardrail #3) is NOT required for it;
     any **machine-consumed control field** (action enum, recipient,
     send-decision) MUST be constrained.
   * **Heavy section reads** (EDI-850, knowledge-graph,
     change-analysis) ship as composer-payload fields by default;
     promote to a dedicated `GET` read projection **only** on an
     ADR-031 trigger (see item 5).
   * `POST /api/v1/sandbox/simulate-inbound` (sandbox only) —
     **must** carry a non-prod `tenant_id` and be excluded from the
     prod hash chain; a test asserts sandbox-injected records cannot
     acquire a prod tenant or append to the prod `audit_trail`.
   Emit `case_update` / `case_close` + pipeline-step events via
   `api/case_events.py` / `api/routes/ws.py`.
7. **OpenAPI:** regenerate `openapi/asoe2.openapi.json`; the UI's
   `npm run generate-types` round-trips it and CI `verify-types`
   gates drift.

### 2.3 Frontend workstream (summary; full detail in the paired plan)

The inbox becomes an **`EMAIL_ENTRY` lens on the two-pane `/cases`
workspace** (filter chip + existing master-detail), not a revived
route. New detail sections mount **inside `ExceptionDetailPanel`**
(the record picker is now stacked atop the detail pane after
`662c9d2`) as dumb `EvidenceBlock` projectors using
`AgentReasoningCard` (Layer 1/2), enums from `useHealth`, SVG graphs
(dagre for the constraint graph; radial/SVG for the knowledge
graph), design tokens only. Full breakdown, component list, and test
gates live in `asoe-ui/docs/customer-inbox-implementation-plan.md`.

## 3. Phased delivery

Each phase is one PR pair (asoe2 + asoe-ui), draft, with the
regression / architectural locks both `CLAUDE.md` files mandate.
Re-prioritised per the Domain/PO panel: **the minimum lovable slice
is Phases 0–3** (lens + AI analysis + order entry — the actual
order-to-cash throughput win). Draft Reply (high value) is pulled
ahead of the graphs (evidence-richness, deferrable).

| Phase | Scope | Gated by |
|---|---|---|
| 0 | Contracts + schemas (+ `autonomy_vocab_version`) + OpenAPI regen + UI type-gen; `EMAIL_ENTRY` filter; resolve the `exceptions.ts` (L1–L3) vs `generated.ts` (L1–L4) type drift; add health fields for autonomy ordering / `email_classification` / constraint statuses | type round-trip green; **vocab-version hard-gate test** |
| 1 | Inbox lens on `/cases` (filter chip, master-detail) | browser e2e |
| 2 | AI Analysis / Entities / SAP Data sections | composer + section locks |
| 3 | Order Entry (extract gateway + recipe + corrections + ERP submit) — **MLS completes here** | deterministic recipe test + Shadow + cosign(>$10k) |
| 4 | Draft Reply + Simulate Inbound + live pipeline (WS) | Shadow + cosign; sandbox-isolation test |
| 5 | EDI 850 builder + section | pure-function unit tests |
| 6 | Change Analysis (variable-cardinality constraints, recipe-homed) | deterministic recipe test + composer |
| 7 | Constraint Graph + Knowledge Graph (deferrable behind demand) | graph render |
| 8 | Hardening — full test pyramid, axe, contract snapshots, docs/ADR status → Accepted | all gates |

## 4. Why not revive the standalone `/inbox` screen?

The 2026-05 PO review + ADR-041 already judged a parallel inbox
"paying rent on a synonym" of `/cases`. Operators work **cases**
(the SOX-audit boundary); an email is simply the origin of an
`EMAIL_ENTRY` case. Reviving `/inbox` would re-introduce the
two-queue inconsistency ADR-041 removed and duplicate the
case-switch-race, pin-selection, and WS-refresh guards already
hardened on `/cases`. The prototype's *features* are valuable; its
*screen topology* is not. (This supersedes the "enhance `/inbox`"
recommendation in `asoe-ui/docs/prototype_gap_analysis.md` §9, which
predates ADR-041.)

A faithful 1:1 standalone port and a hybrid shell were both
considered and rejected for the same reason; integration into the
case workspace was selected.

## 5. Decision: autonomy L1–L4 ordering (Product directive, 2026-05-23)

**Verified facts** (read from code, not the prototype):
* `contracts/policy.py:46,226` — backend canonical: **L4 = full
  autonomy (auto-execute), L3 = act & inform, L2 = recommend, L1 =
  observe only**. So L1 = *lowest* autonomy, L4 = *highest*.
* `asoe-ui/src/lib/cases.ts::AUTONOMY_LEVEL_DESCRIPTIONS` — L1
  "Block automatically" … L4 "Fully automated". **The UI already
  matches the backend** (L1 low → L4 high); it is *not* the inverted
  artifact.
* Prototype (`MB_AUTON` / AI-analysis descriptions) — **L1 = fully
  autonomous auto-reply … L4 = escalate to human**. The prototype is
  the inverted ladder (L1 = highest autonomy).

**Product decision:** adopt the **prototype's L1–L4 ordering**
(L1 = most autonomous → L4 = human) as the operator-facing semantics.

**Why this is not a trivial relabel — and how it is done safely.**
`autonomy_level` is **audit-bearing** (`audit_bearing_registry.yaml`;
SOX §404 narratives cite the L-tiers) and **load-bearing** across
`DUPLICATE_PO_AUTONOMY_LEVELS`, `EDI_MISMATCH_AUTONOMY_LEVELS`,
`MANUAL_ORDER_INTAKE_AUTONOMY_LEVELS`, and the ADR-034 L4→L3
demotion. The Compliance + Architect panel verdict: **inverting the
canonical numbers in place would silently flip the meaning of every
historical "L1"/"L4" attestation — an audit-integrity catastrophe.**
Therefore the directive is implemented as a **versioned vocabulary**,
not an in-place mutation:

1. Introduce `autonomy_vocab_version`. The new prototype ordering is
   **v2**; existing records keep resolving under **v1** — the
   append-only audit chain is **never** rewritten.
2. The ordering, labels, and a numeric `rank` are served from the
   **health payload** (`allowed_autonomy_levels: {level, label,
   rank}[]`). The UI sorts/render by `rank` (no hardcoded map →
   Guardrail #1 satisfied); `cases.ts` keeps only a transition
   fallback.
3. The numeric→behaviour gating that recipes dispatch on is migrated
   coherently with v2 so "L1" the operator sees and "L1" the engine
   gates agree under one version.
4. **Gates:** explicit Compliance sign-off + dual-control on the
   policy change, and a Phase-0 hard-gate test asserting
   version-correct resolution of historical vs new records. Until
   those clear, this remains *Proposed*, not ratified.

## 5b. Other edge cases / open questions
* **Real constraint data coverage.** The 10 checks depend on
  `constraints/`, `knowledge/`, and gateway coverage that may not
  all exist. Missing audit-bearing fields are registered under
  `grandfather_clauses`, never silently dropped (Guardrail #6/#7).
* **`EMAIL_ENTRY` correlation keys.** ADR-041 deferred the hard
  "EMAIL_ENTRY ⇒ source_email_id required" invariant; the
  production ingestion path (Phase 1) must populate it before that
  rule can turn on.
* **`invoice_query` has no `email_classification` target** (Domain
  correction). ADR-041's set is `{NEW_ORDER, ORDER_CHANGE, INQUIRY,
  COMPLAINT, OTHER}`; the prototype's `invoice_query` (an AR /
  billing dispute) would silently collapse into `OTHER`. Decide in
  Phase 0: add `INVOICE_QUERY` (preferred — distinct AR workstream)
  or document the `OTHER` mapping in ADR-041 §2 so triage routing
  isn't surprised.
* **Graphs may be low-value.** `ConstraintGraph` / `KnowledgeGraph`
  partly duplicate the existing `/exceptions/{id}/trace` + pipeline
  topology (ADR-027). Treated as evidence-richness; deferrable
  behind real demand (see §3 re-prioritisation).
* **Demo-only niceties** (open-in-new-tab, right-click context menu,
  copy-sender) are low-value; proposed for drop/defer.
* **Scope boundary.** Quota Mgmt, Performance, Admin, CSR Chat
  (gap-analysis §5/§6) are out of scope for this ADR.

## 5c. Panel review (2026-05-23)

Four-lens independent review (Architect, Compliance, Frontend,
Domain) of the draft. Adopted corrections, all folded in above:

* **Architect:** Change Analysis was mis-homed — `constraints/` is
  the constrained-generation router and `agents/harness.py` is a
  gated single-case loop; constraint evaluation moved into
  **recipes** (§2.2.4). Composer stays sole assembler but heavy
  sections become **lazy per-section reads** (§2.2.5). Target the
  canonical `MANUAL_ORDER_INTAKE` recipe, not the legacy stub.
* **Compliance (veto holder):** order-entry submit made
  Shadow+cosign explicit; corrections split into immutable audit vs
  consented retraining; draft-reply `body_hash` persisted; sandbox
  isolation test required; each grandfather clause needs an id +
  deadline. Autonomy: **endorsed only as a versioned vocabulary** —
  no historical mutation, compliance sign-off + dual control (§5).
* **Frontend:** sections mount inside `ExceptionDetailPanel`'s
  data-presence list (lazy), not the case header;
  `ChangeAnalysisSection` must be split; autonomy ordering served
  from `health.allowed_autonomy_levels` (rank-sorted), not a
  hardcoded UI map; dagre for the constraint graph but radial/SVG
  for the knowledge graph; resolve L3-vs-L4 type drift in Phase 0.
  (Detail in the paired asoe-ui plan §8.)
* **Domain/PO:** `invoice_query` classification gap (§5b);
  render variable-cardinality constraints, drop agent timings as
  evidence; MLS = Phases 0–3; Draft Reply pulled ahead of graphs
  (§3).

## 5d. Architecture-fidelity audit (2026-05-23)

A follow-up audit against the live codebase caught the draft drifting
from current architecture in three places; all corrected above:

1. **Bespoke `/cases` write verbs → the disposition surface.**
   `api/routes/cases.py` is read-only by design (writes go through
   override / cosign / disposition). Order-entry submit, reply send,
   and corrections are now modelled as **dispositions /
   recipe-executions on the per-record exception surface** (+ cosign),
   not new `/cases/{id}/...` POST/PATCH verbs. Extraction and
   draft-generation are reads/enrichment, not writes (§2.2.6).
2. **Knowledge Graph mis-homed to `knowledge/`.** That package is
   `compaction / policy / shadow_llm / skills`, not a graph producer.
   The KG is a net-new derived projection (or deferred); the
   Constraint Graph reuses `/exceptions/{id}/trace` + topology
   (§2.1).
3. **Per-section read endpoints pre-empted ADR-031.** Sections are
   composer-payload fields by default; a dedicated read projection
   is split out **only on a pre-committed ADR-031 trigger** (§2.2.5).

Plus the two-pane correction: asoe-ui `662c9d2` collapsed the
3-column layout to two panes (record picker inline in the detail
pane); all pane references updated (§1, §2.3).

## 6. Definition of Done

ADR-042 moves to **Accepted** when:
* All section schemas exist with audit-registry entries (each
  grandfather clause carrying an id + compliance-approved deadline)
  + validator tests; OpenAPI round-trips and `verify-types` is green.
* The `EMAIL_ENTRY` lens + detail sections render from live backend
  data on `/cases`, no client-side business logic.
* Order-entry extraction, EDI 850, constraint evaluation, draft
  reply all execute server-side, Shadow-gated, with deterministic
  recipe test paths and constrained-generation locks.
* Compliance Shadow + **cosign(>$10k) enforced on every financially
  binding write** (order-entry submit, draft-reply send); RBAC +
  tenant scoping on every new route.
* Autonomy v2 vocabulary shipped with `autonomy_vocab_version`,
  compliance sign-off + dual-control, and a version-resolution
  hard-gate test; **no historical audit record rewritten**.
* Full test pyramid per both `CLAUDE.md` test-strategy gates
  (unit + architectural locks + browser operator journeys + axe).
* Docs updated (`ARCHITECTURE.md`, `prototype_gap_analysis.md`
  cross-link, this ADR).

---

*End of ADR-042.*
