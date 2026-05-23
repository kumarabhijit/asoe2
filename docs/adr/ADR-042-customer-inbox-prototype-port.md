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
`/cases` two-/three-pane workspace is the single canonical surface.
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
| Change Analysis (10 constraints / 7 agents / scenarios / decision / financials) | `constraints/` evaluations + `agents/harness.py` multi-agent run + Compliance Shadow + composer | Harness ✅; mapping ⛔ |
| Constraint Graph | `orchestration/graph.py::get_pipeline_topology` + per-record `/exceptions/{id}/trace` (ADR-027) | Topology ✅; per-case projection ⛔ |
| Knowledge Graph | `knowledge/` subsystem → entity-relationship payload section | Partial ⛔ |
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
3. **Recipes** (`recipes/`): `EMAIL_ORDER_ENTRY` submit-to-ERP
   (BAPI / EDI 850), draft-reply action. Recipes return dicts; the
   composer projects them — no "ready-to-render" composition inside
   recipes (Guardrail #6).
4. **Constraints** (`constraints/`): map the prototype's 10 checks
   (Inventory ATP, Production, Transport, Warehouse, Order
   Lifecycle, SLA, Financial, Dependencies, Network, Priority) onto
   the constraints subsystem; financial >$10k stays the cosign
   threshold (ADR-040). Multi-agent timings via `agents/harness.py`.
5. **Orchestration & composer:** extend `build_analysis`
   (`orchestration/nodes.py`) — the **sole** assembler — to populate
   the new sections through `api/analysis_composer.py` /
   `analysis_adapters.py`. Ensure `orchestration/graph.py` topology
   covers the order-entry path.
6. **API routes** (extend, don't fork — mirror the RBAC + tenant +
   `_scope_to_user` shape in `api/routes/cases.py`):
   * `GET /cases?case_type=EMAIL_ENTRY` filter (list lens).
   * `POST /cases/{id}/order-entry/extract`
   * `PATCH /cases/{id}/order-entry` (operator corrections → audit/retraining)
   * `POST /cases/{id}/order-entry/submit` (Shadow-gated)
   * `POST /cases/{id}/reply/draft`, `POST /cases/{id}/reply/send` (Shadow + cosign)
   * `GET /cases/{id}/edi-850`, `GET /cases/{id}/knowledge-graph`
   * `POST /api/v1/sandbox/simulate-inbound` (sandbox only).
   Emit `case_update` / `case_close` + pipeline-step events via
   `api/case_events.py` / `api/routes/ws.py`.
7. **OpenAPI:** regenerate `openapi/asoe2.openapi.json`; the UI's
   `npm run generate-types` round-trips it and CI `verify-types`
   gates drift.

### 2.3 Frontend workstream (summary; full detail in the paired plan)

The inbox becomes an **`EMAIL_ENTRY` lens on `/cases`** (filter chip
+ existing master-detail), not a revived route. New colocated
`/cases` detail sections (`OrderEntrySection`, `Edi850Section`,
`EntitiesSection`, `SapDataSection`, `ConstraintGraphSection`,
`KnowledgeGraphSection`, `DraftReplyPanel`) are dumb `EvidenceBlock`
projectors using `AgentReasoningCard` (Layer 1/2), enums from
`useHealth`, graphs via `dagre`, design tokens only. Full breakdown,
component list, and test gates live in
`asoe-ui/docs/customer-inbox-implementation-plan.md`.

## 3. Phased delivery

Each phase is one PR pair (asoe2 + asoe-ui), draft, with the
regression / architectural locks both `CLAUDE.md` files mandate.

| Phase | Scope | Gated by |
|---|---|---|
| 0 | Contracts + schemas + OpenAPI regen + UI type-gen; `EMAIL_ENTRY` list filter | type round-trip green |
| 1 | Inbox lens on `/cases` (filter chip, master-detail) | browser e2e |
| 2 | AI Analysis / Entities / SAP Data sections | composer + section locks |
| 3 | Order Entry (extract gateway + recipe + constraints + corrections + ERP submit) | deterministic recipe test + Shadow |
| 4 | EDI 850 builder + section | pure-function unit tests |
| 5 | Change Analysis + Constraint Graph | constraints + topology |
| 6 | Knowledge Graph | knowledge payload |
| 7 | Draft Reply + Simulate Inbound + live pipeline (WS) | Shadow + cosign; sandbox flag |
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

## 5. Edge cases / open questions

* **Autonomy semantics conflict (must resolve in Phase 0).** The
  prototype's L1–L4 runs auto → human; the backend's
  `contracts/policy.py` / `src/lib/cases.ts::autonomyLevelLabel`
  runs block → fully-automated (inverted). One source of truth wins
  (`contracts/policy.py`); the prototype labels are discarded. This
  is a silent-bug magnet — flagged explicitly.
* **Real constraint data coverage.** The 10 checks depend on
  `constraints/`, `knowledge/`, and gateway coverage that may not
  all exist. Missing audit-bearing fields are registered under
  `grandfather_clauses`, never silently dropped (Guardrail #6/#7).
* **`EMAIL_ENTRY` correlation keys.** ADR-041 deferred the hard
  "EMAIL_ENTRY ⇒ source_email_id required" invariant; the
  production ingestion path (Phase 1) must populate it before that
  rule can turn on.
* **Demo-only niceties** (open-in-new-tab, right-click context menu,
  copy-sender) are low-value; proposed for drop/defer.
* **Scope boundary.** Quota Mgmt, Performance, Admin, CSR Chat
  (gap-analysis §5/§6) are out of scope for this ADR.

## 6. Definition of Done

ADR-042 moves to **Accepted** when:
* All section schemas exist with audit-registry entries + validator
  tests; OpenAPI round-trips and `verify-types` is green.
* The `EMAIL_ENTRY` lens + all seven detail sections render from
  live backend data on `/cases`, no client-side business logic.
* Order-entry extraction, EDI 850, constraint evaluation, draft
  reply all execute server-side, Shadow-gated, with deterministic
  recipe test paths and constrained-generation locks.
* Compliance Shadow + cosign enforced on every write; RBAC + tenant
  scoping on every new route.
* Full test pyramid per both `CLAUDE.md` test-strategy gates
  (unit + architectural locks + browser operator journeys + axe).
* Docs updated (`ARCHITECTURE.md`, `prototype_gap_analysis.md`
  cross-link, this ADR).

---

*End of ADR-042.*
