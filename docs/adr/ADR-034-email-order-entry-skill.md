# ADR-034 — Email Order Entry Skill: Bucketed Mapping, Phase Plan, and Halt-Conditioned Deferrals

**Status:** Proposed
**Date:** 2026-05-04
**Decision driver:** Product Owner spec `knowledge/skills/email-order-entry/specs/order_entry_spec.md`
**Authoring path:** `prompts/po-spec-to-asoe.md` STEP 0 (Spec Analysis Gate) applied to the PO spec.
**Reviewers required:** AI/LangGraph → Compliance → Tools Admin → Frontend Platform → Compliance veto holder.

---

## 1. Context

The PO submitted an `order_entry_skill_documentation.md` spec describing a domain skill
(`asoe-om-order-entry`) for converting non-EDI email orders into ERP sales orders. The
spec spans intake (email + PDF + Excel + image extraction), entity resolution, validation
suite, ERP simulation, ERP submit, MCP tool dependencies, autonomy graduation, calibration
metrics, and tech stack.

This is the second large PO spec to be received under the v4 architecture. The first
(`docs/specs/duplicate-po-product-spec.md`) was converted via the `prompts/po-spec-to-asoe.md`
playbook into:

* an intent (`DUPLICATE_PO`)
* a recipe (`recipes/DuplicatePORecipe.py` — pure deterministic scoring + decision tree)
* a skill (`skills/duplicate-po_SKILL.md`)
* gateway dependencies + effects on the registry
* an autonomy mapping in `contracts/policy.py`
* tests + UI mirror types

This ADR documents the same conversion for the new spec.

---

## 2. STEP 0 — Spec Analysis Gate (bucketed mapping)

Per `prompts/po-spec-to-asoe.md` STEP 0, every section of the PO spec is classified into
SKILL territory, RECIPE territory, REFERENCE / PRODUCT-SPEC territory, or — added by this
ADR — GATEWAY territory (infrastructure I/O the recipe is forbidden from doing inline).

### 2.1 Mapping table

| Spec section | Bucket | Destination |
|---|---|---|
| §1 Overview, Skill Metadata, Use Cases, Routing Notes | SKILL | `skills/email-order-entry_SKILL.md` (intent name, description, recipe selection, routing notes verbatim where they affect classifier behaviour) |
| §2 Problem Domain, Friction Points | REFERENCE | Already preserved verbatim in `knowledge/skills/email-order-entry/specs/order_entry_spec.md` |
| §3 8-step pipeline (steps 1, 2, 3, 4, 5, 7, 8) | GATEWAY + REFERENCE | Steps 1–5 + 7–8 are I/O: tenant policy resolution, artifact collection, multi-format extraction, normalisation, entity resolution, ERP simulation, ERP submit. **Recipes never call gateways directly** (CLAUDE.md §1, ADR-025). These become declared `GatewayDependency` entries on the recipe spec and are resolved in `resolve_dependencies` *before* shadow_audit. Stub gateways are deferred to a follow-up PR; this PR ships the declarations and asserts `required_for_audit=False` so a missing gateway routes to `AUDIT_CONTEXT_MISSING` (a typed terminal state per Verdict 2026-04-22) rather than crashing the run. |
| §3 Confidence Scoring + L2 Behavior Thresholds | RECIPE | The deterministic core of this skill. Inputs `composite_confidence`, `validation_failures`, plus thresholds → outputs `classification`, `recommended_action`, `autonomy_level`. Lives in `recipes/EmailOrderEntryRecipe.py`. Thresholds (0.95 / 0.85 / 0.99 / fatal-floor) live in `contracts/policy.py`. |
| §4 Resolution Workflows (6 paths) | RECIPE (action vocabulary) + SKILL (routing instruction) | Adds 6 new resolution actions to `AllowedResolutionAction` Literal: `ONE_CLICK_APPROVE`, `STANDARD_REVIEW`, `LOW_CONFIDENCE_FLAG`, `AUTO_CORRECT`, `REQUEST_CLARIFICATION`, `REJECT`. (`ESCALATE` already exists.) `AUTO_RETRY` from the spec is *primitive-internal* and never surfaces as a recipe-level action — it lives inside the entity-resolution gateway's fallback chain. |
| §4 Non-Disable-able Floor | RECIPE (input contract) + COMPLIANCE | The four floor checks (sender auth, duplicate PO, credit block, customer identity) are *evidence acquisition* gateways. They run in `resolve_dependencies` before shadow_audit (ADR-025 / Pillar 1). The recipe enforces a hard rejection when `non_disableable_floor_breached=True` is present in the floor inputs — this is policy that may not be tenant-overridden, so it lives in the recipe. |
| §5 Autonomy Levels (L1–L4) | RECIPE (consumed) + POLICY (defined) | `EMAIL_ORDER_ENTRY_AUTONOMY_LEVELS` mapping in `contracts/policy.py`, injected via `validate_types` and consumed by the recipe (mirrors `DUPLICATE_PO_AUTONOMY_LEVELS`). |
| §6 Primitive Dependencies (cheap + expensive) | GATEWAY | Each primitive is a gateway operation. Cheap = `required_for_audit=True` for the four floor checks; expensive = `required_for_audit=False` so an outage routes to `AUDIT_CONTEXT_MISSING` rather than crashing. Stub gateway ships in a follow-up PR; the registry declares names + ops in this PR. |
| §6 MCP Tool Dependencies | GATEWAY (deferred to v4.1+) | MCP integration is platform-track work. Documented in §6 of this ADR as a Phase D backlog item. The recipe never references MCP. |
| §7 Metrics & Graduation | REFERENCE | Calibration / graduation belongs to ADR-032's existing calibration deferral track. Not in scope for this ADR's first phase. |
| §8 Tech Stack | REFERENCE | Documented; no impact on backend/recipe code. |

### 2.2 The "single intent" decision

The PO spec describes a multi-step *pipeline* that ultimately produces a sales order, but
many of the validation primitives in §6 (MOQ, OverMax, Credit, Pricing variance, Duplicate
PO, Delivery date feasibility) **already have V1 recipes** in this codebase
(`MOQRoundUpRecipe`, `OverMaxTrimRecipe`, `CreditHoldReleaseRecipe`,
`PriceAdjustmentRecipe`, `DuplicatePORecipe`, `DeliveryDelayResolutionRecipe`).

Per `prompts/po-spec-to-asoe.md` STEP 1, a recipe may not bundle multiple intents. The
`EMAIL_ORDER_ENTRY` intent therefore does **not** re-implement those primitives. It scores
the *post-extraction*, *post-validation* envelope and decides the order's terminal action
on the email-intake leg (auto-approve, review, low-confidence, fatal). The primitive-level
validations remain their own intents and run as their own graph executions if/when an
order line later trips them in the ERP path. This is the cleanest mapping under the
existing single-intent-per-graph-run topology.

### 2.3 HALT conditions (per `po-spec-to-asoe.md` §HALT)

| HALT # | Condition | Applies? | Resolution |
|---|---|---|---|
| 1 | Recipe logic requires external API / DB / queue | **YES** — extraction, MCP, entity resolution, ERP simulate/submit all I/O | Move all I/O behind declared `GatewayDependency` entries (§4.2 of this ADR). Recipe stays pure. |
| 2 | More than one autonomy tier and unclear GREEN/YELLOW/RED mapping | NO — autonomy is a recipe output that orchestrates routing alongside (not against) Compliance Shadow verdicts; same mechanism as `DuplicatePORecipe`. | n/a |
| 3 | Single intent value cannot be picked because spec is a multi-step workflow | **YES** — spec spans many existing intents | Scope this PR to one intent (`EMAIL_ORDER_ENTRY`) targeting *only* the post-extraction envelope-level decision. Other intents remain on their own graph runs. |
| 4 | New first-class field needed on `GraphState`/`OrderEvent` | NO — `event.metadata` carries `composite_confidence`, `extraction_evidence`, `validation_failures`, `non_disableable_floor_breached`, `customer_id_resolution`. Same escape-hatch as DuplicatePO's `signal_scores`. | n/a |
| 5 | Spec describes feedback / retraining loop | **YES** — §7 graduation gates + calibration metrics | Defer to ADR-032's existing calibration-deferral track. Not in this PR. |
| 6 | Threshold is ambiguous about fixed-policy vs tenant-config | The 0.95 / 0.85 / 0.99 numbers are fixed *platform defaults* per the spec's L2 Behavior table. Tenant overrides via `tenant_config` gateway are deferred to ADR-030 (config override hierarchy) — same treatment as DuplicatePO weights. | Platform defaults shipped this PR; per-tenant overrides land via ADR-030 in a follow-up. |

---

## 3. Expert Recommendations Memo (consulted perspectives)

The PO instruction was: *"Discuss it with experts and identify their recommendations."*
The architecture's existing review-board chain (AI/LangGraph → Compliance → Tools Admin →
Frontend Platform → Compliance veto holder) is the standing forum. Below is a per-perspective
read of the spec, recorded for the formal review.

### 3.1 Principal AI/LangGraph Architect

* **Verdict:** Supports the carve-out to a single `EMAIL_ORDER_ENTRY` intent. Rejects
  any reading of the spec that would inline extraction or ERP submit into the recipe;
  both violate CLAUDE.md §1 (recipes never do I/O) and would break the determinism
  guarantee the audit trail relies on.
* **Recommendations:**
  1. Recipe consumes a *post-extraction envelope* — `composite_confidence`,
     `validation_failures` list, `non_disableable_floor_breached` boolean, and
     `customer_resolution_status` — never raw email content.
  2. Extraction runs *before* the graph (an upstream `email-intelligence-agent`
     produces the `OrderEvent` with extracted fields on `metadata`); the V1 surface
     records the hand-off as a typed event_type `EMAIL_ORDER_ENTRY_REQUEST`.
  3. The four "non-disable-able floor" gateways must be declared with
     `required_for_audit=True` so a gateway outage halts the run rather than
     silently producing a low-confidence record.

### 3.2 Compliance Veto Holder (Verdict 2026-04-22)

* **Verdict:** Approves with conditions. The recipe must produce evidence-bearing fields
  for the SOX-relevant decision payload; "—" / "N/A" placeholders are unacceptable
  (Pillar 3). The four floor checks are audit-bearing and must produce evidence, even
  when the order is ultimately rejected.
* **Conditions:**
  1. Add an `EmailOrderEntryAnalysisData` Pydantic mirror in `api/schemas.py` and the
     matching TypeScript type in `asoe-ui/src/types/exceptions.ts`. Fields: composite
     confidence, per-field confidence breakdown, classification, recommended action,
     autonomy level, validation failures (typed list), floor-check results
     (sender_authorized, duplicate_po_clear, credit_clear, customer_resolved), and a
     reference to the upstream extraction trace ID.
  2. Register the new section in `compliance/audit_bearing_registry.yaml` with
     audit-bearing fields enumerated. Until the gateway-backed evidence path lands,
     classify the section under `grandfather_clauses` with a compliance-approved
     deadline.
  3. The terminal `REJECT` action must carry a typed `reject_reason_code`
     constrained to a closed vocabulary (sender_unauthorized, corrupt_input,
     credit_block, etc.).

### 3.3 Tools Admin / SRE

* **Verdict:** No objection to the carve-out. Notes the registry will gain a non-trivial
  number of new gateway operations (8+); recommends staging the gateway stubs across
  two PRs (declarations now, stubs in follow-up) so review surface stays auditable.
* **Recommendations:**
  1. ADR-026's event-driven ingestion path (Proposed) is the right landing surface
     for inbound emails once shipped. Until then, the existing
     `POST /api/v1/exceptions/resolve` is the entry point.
  2. The MCP integrations described in §6 of the spec are platform-track work. Defer
     to a separate ADR (proposed name: ADR-035 — MCP Tool Dependency Surface) under
     the same review chain.

### 3.4 Frontend Platform

* **Verdict:** No objection. Will mirror the Pydantic shape into TypeScript types and
  rely on `<EvidenceBlock>` for the three legal presence states (Pillar 3). Will *not*
  fabricate confidence pills client-side; backend is the source of truth.
* **Recommendation:** Defer the dedicated `EmailOrderEntrySection` UI component to a
  follow-up; this PR ships the type and the registry entry so coverage tests pass and
  the section can be rendered as `Context Not Required for Resolution` until the
  gateway-backed evidence is wired up.

### 3.5 Domain SME (CPG order management)

* **Verdict:** Concurs with the spec's definition of the four non-disable-able floor
  checks and the L1–L4 autonomy ladder. Notes the L4 default in the spec is "not
  recommended at launch" — recipe must enforce L4 → L3 demotion automatically until a
  graduation event lands (ADR-032 calibration track).

---

## 4. Phase Plan / Backlog

The work is decomposed into five phases. **Phase A** is shipped in this PR; **B–E** are
backlog tracked here for the review board.

### Phase A — Skill, recipe, vocabulary sync, tests, UI types (THIS PR)

* `skills/email-order-entry_SKILL.md`
* `recipes/EmailOrderEntryRecipe.py` (pure deterministic — no I/O)
* `contracts/models.py::Intent` += `EMAIL_ORDER_ENTRY`
* `constraints/specs.py::AllowedIntent` += `EMAIL_ORDER_ENTRY`
* `constraints/specs.py::AllowedRecipeName` += `EmailOrderEntryRecipe.py`
* `constraints/specs.py::AllowedResolutionAction` += `ONE_CLICK_APPROVE`,
  `STANDARD_REVIEW`, `LOW_CONFIDENCE_FLAG`, `AUTO_CORRECT`, `REQUEST_CLARIFICATION`,
  `REJECT`
* `recipes/registry.py` += `RecipeSpec` for `EmailOrderEntryRecipe.py` with the four
  floor-check `GatewayDependency` declarations (`required_for_audit=True`) and the
  expensive checks (`required_for_audit=False`). Stub implementations land in Phase B.
* `contracts/policy.py` += thresholds + autonomy mapping + reject reason vocabulary.
* `skills/loader.py::select_for_event` += `EMAIL_ORDER_ENTRY` branch.
* `orchestration/nodes.py::validate_types` += branch.
* Tests:
  * `tests/test_recipes.py::TestEmailOrderEntryRecipe`
  * `tests/test_registry.py::TestEmailOrderEntrySpec`
* UI: `asoe-ui/src/types/exceptions.ts` += `EmailOrderEntryAnalysisData` interface.

### Phase B — Gateway stubs (follow-up PR)

* `gateways/stub.py` += stubs for the 8 declared gateway operations.
* `tests/conftest.py` += stub registration so e2e tests can run without network.
* Unit tests for each stub.
* `compliance/audit_bearing_registry.yaml` += `EmailOrderEntryAnalysisData` rows
  (initially under `grandfather_clauses` with a 90-day deadline).

### Phase C — UI section component + EvidenceBlock projection (follow-up PR)

* `asoe-ui/src/app/exceptions/EmailOrderEntrySection.tsx` — dumb projector
  rendered via `OrderAnalysis.email_order_entry_analysis` (data-presence
  dispatch — no page-level branching, preserves CLAUDE.md Guardrail #1).
* Mock exception in `asoe-ui/src/lib/api.ts MOCK_EXCEPTIONS` with
  `intent="EMAIL_ORDER_ENTRY"` so the search has a real target and the
  section has something to render in demos.
* Search short-circuit: SCREAMING_SNAKE_CASE single-token queries in the
  exception list pane auto-promote to an `intent:` operator instead of
  going through Fuse fuzzy match, fixing the class of "EMAIL_ORDER_ENTRY
  matches MIN_ORDER_QTY by edit distance" surprise.
* Architectural lock test ensuring the section renders by data-presence
  alone (no `if (intent === ...)` dispatch).
* **Out of scope for Phase C:** any changes to `/inbox`. The IA decision
  recorded under §6 (below) keeps `/inbox` and `/exceptions` as separate
  surfaces; productizing the inbox is Phase F.

### Phase D — MCP tool surface (ADR-035, separate review)

* Defines how MCP tool calls (`resolve_customer_by_email`, `simulate_sales_order`,
  `create_sales_order`, etc.) are exposed as gateway operations.
* Production-grade ERP integration is a separate platform track; current production
  remains StubGateway-backed.

### Phase E — Calibration & graduation (under ADR-032)

* The §7 graduation metrics feed the ADR-032 calibration loop. No separate ADR needed.
* L4 → L3 auto-demotion enforced in `policy.py` until calibration ships graduation
  signals.

### Phase G — Unified Detail Surface (PO-driven IA correction, ships now)

Per §6 (corrected), this phase delivers the surface-level merge: the CSA
sees one detail page for the "email → ERP resolution" task regardless
of which list view brought them there. Three commits:

* **G.1 (asoe2 backend):**
  - `api/schemas.py` += `EmailSourceData` (from_address, received_at,
    subject, body_hash, attachment_manifest, body_excerpt) + new
    `OrderAnalysis.email_source` field.
  - `recipes/registry.py`: extend the `EmailOrderEntryRecipe` spec's
    `email_intake` gateway with a `fetch_message` operation
    (`required_for_audit=True`); result_key=`email_source_context`.
  - `api/analysis_adapters.py` += `adapt_email_source` registered as a
    SECONDARY adapter on `EmailOrderEntryRecipe.py` (same attestation
    target as the primary `email_order_entry_analysis` per the
    SECONDARY pattern used for `DuplicatePORecipe.py → order_comparison`).
  - `compliance/audit_bearing_registry.yaml`: 6 new rows for
    `EmailSourceData` (5 audit-bearing, 1 contextual). Tally bumped.
  - `tests/conftest.py` + `api/sandbox_gateways.py`: stub
    `fetch_message` response.
  - Tests: adapter unit + e2e graph.

* **G.2 (asoe-ui section):**
  - `src/types/exceptions.ts` += `EmailSourceData` mirror + field on
    `OrderAnalysis`.
  - `src/app/exceptions/EmailSourceSection.tsx`: dumb projector mounted
    above `EmailOrderEntrySection` via data-presence dispatch.
  - `src/lib/api.ts MOCK_EXCEPTIONS["exc-026"]`: extend with
    `email_source` payload so the section renders in demos.
  - Component test + architectural lock test.

* **G.3 (asoe-ui inbox bridge) — superseded 2026-05-10, see §6.1.**
  - Original behaviour: `/inbox` NEW_ORDER rows whose `exception_id`
    was set deep-linked to `/exceptions/{exception_id}` on click.
  - Current behaviour (PO supersession 2026-05-10): every `/inbox`
    row selects locally (consistent master-detail UX). The right-pane
    detail renders an explicit "Open in Exception Queue" jump button
    when `selected.exception_id` is present; the operator chooses
    when to leave the inbox surface. The button pushes
    `/exceptions/{exception_id}?from=inbox` so the detail page
    renders "Back to Inbox" instead of the default "Back to Queue".
  - Exception Queue detail surfaces a "View source email" back-link
    when `event.metadata.source_email_id` is present, navigating to
    `/inbox?msg={id}`. (Unchanged.)
  - Inbox-side change in scope: the row-click dispatch + the new
    jump-button affordance on the right pane. The page's static-mock
    substrate stays intact (productisation is still Phase F /
    proposed ADR-036).

### Phase F — Inbox productisation (deferred, separate ADR pointer)

* **Proposed name:** ADR-036 — Email Intake Surface and Bridge to Exception Queue.
* Productizes the current `/inbox` page (today a hand-coded mock) to consume
  the upstream `email-intelligence-agent` output, source category vocabulary
  from `useHealth`, and bidirectionally deep-link with the Exception Queue:
  inbox `NEW_ORDER` row that produced an `EMAIL_ORDER_ENTRY` exception →
  `/exceptions/{id}`; exception detail with `event.metadata.source_email_id`
  → back to the inbox row.
* **Why a separate ADR:** the inbox page covers more categories than just
  PO intake (`SHIPMENT_INQUIRY`, `INVOICE_QUERY`, `COMPLAINT`, `ORDER_CHANGE`,
  `NEW_ORDER`); it is the operator's browse-inbound surface and is upstream of
  this ADR's exception-queue scope. Bundling its productisation into ADR-034
  would conflate intake with resolution.
* **Reviewer chain (proposed):** AI/LangGraph → Compliance → Tools Admin →
  Frontend Platform → Compliance veto holder.

---

## 5. Acceptance for Phase A

* `python -m pytest` is green (no failing tests).
* `pre-commit` hooks (if any) pass.
* The vocabulary sync invariants in `tests/test_constraints.py` and
  `tests/test_registry.py` continue to hold (these tests derive expectations
  dynamically from the literals/registry, so the count update is automatic).
* The new recipe contains no I/O, no LLM calls, no side effects (`grep` rule).
* The spec file is preserved at `knowledge/skills/email-order-entry/specs/order_entry_spec.md`,
  not under `skills/`.

---

## 6. Information-Architecture Decision (corrected post-PO review — Phase G)

A follow-up review with the Product Owner (2026-05-05) **superseded the
original §6 decision**. The PO's binding premise: the CSA's lived workflow
is *one* task ("process this customer email through to ERP resolution")
that the legacy ERP split across two human roles (junior reads + enters,
senior resolves). ASOE collapses both roles into one human + agent, so the
page count must follow the **task count**, not the legacy role count.

Forcing the CSA to context-switch between a browse-inbound `/inbox` and
a work-the-queue `/exceptions` for the same task — when the system
already has both the email content and the resolution actions — is a
workflow tax paid for an architectural taxonomy the user does not share.

### What stays from the original §6

* **EMAIL_ORDER_ENTRY records that need human review still belong on
  the audited Exception Queue surface** — the SOX requirement is that
  the operator authorising a financially-binding decision sees the
  full audit-bearing evidence payload. That requirement constrains the
  *detail surface*, not the page count.
* **No per-intent page-level dispatch** (CLAUDE.md Guardrail #1). The
  email-source view mounts via the existing data-presence pattern on
  a new `OrderAnalysis.email_source` field; no `if (intent === ...)`.
* **`/inbox` is not deleted.** It still hosts the inbound firehose for
  non-exception categories (`SHIPMENT_INQUIRY`, `INVOICE_QUERY`,
  `COMPLAINT`, `ORDER_CHANGE`). Removing it would lose the
  browse-inbound mode entirely.

### What changes

The detail surface becomes **identical regardless of which list view
got the operator there**. Concretely:

1. A new `<EmailSourceSection>` mounts on the Exception Queue detail
   page above `<EmailOrderEntrySection>`, rendered by data-presence on
   `OrderAnalysis.email_source`. It shows the inbound email's metadata
   (sender, received-at, subject, body hash) and an attachment manifest
   so the operator authorising the order has the source-of-truth
   substrate inline.
2. `/inbox` rows that produced an Exception Queue record deep-link to
   `/exceptions/{id}`. The operator clicks once and lands on the
   unified detail surface — no tab switch, no copy-paste, no re-typing.
3. The Exception Queue detail surfaces a "View source email" back-link
   to `/inbox?msg={source_email_id}` so the navigation is bidirectional.

### Compliance posture

The detail surface gains audit-bearing rows for the email source
(from_address, received_at, subject, body_hash, attachment_manifest).
The `email_intake` gateway gains a `fetch_message` operation
(`required_for_audit=True`) that supplies them. The operator
authorising the action now sees the same source-of-truth substrate the
junior CSA used to manually transcribe.

### Why not Option B (single unified surface)

Option B (delete `/inbox`, route every inbound communication into the
Exception Queue) is the right north star but requires:
* a per-classification audit policy (SHIPMENT_INQUIRY does not need
  `floor_status`; INVOICE_QUERY does not need a financial-impact
  cosign gate),
* an `email-intelligence-agent` integration ADR (today the inbox is a
  hand-coded mock; the upstream classifier is platform-track work),
* a compliance workshop on which inbound categories become SOX-audited.

These are all ratifiable but each is its own ADR. Bundling them into
ADR-034 would re-create the v3 failure mode (writing aspirations into
the spec). Option A unblocks the PO's complaint with one new section
and bidirectional deep-links; Option B becomes a clean follow-up
(proposed ADR-037) once the upstream classifier ships.

### Phase G — Unified Detail Surface (PO-driven IA correction)

This phase is added to §4. The deferred Phase F (inbox productisation
under proposed ADR-036) remains valid: it covers `/inbox`'s upstream
plumbing and the per-classification surface for the non-exception
categories. Phase G is the surface-level merge that solves the CSA's
workflow tax without waiting on F.

---

## 6.1 Supersession (PO ruling 2026-05-10) — Consistent inbox UX + explicit jump button

A second product review (2026-05-10) **partially superseded the
Phase G.3 inbox dispatch behaviour** documented above. The detail
surface unification (§6, §6.G.1, §6.G.2) is unchanged; only the
*navigation* into that surface from `/inbox` changes.

### What the operator reported

Three observations from operator usage:

1. **Inconsistent UX across inbox rows.** Clicking a NEW_ORDER row
   that carries an `exception_id` jumped straight to
   `/exceptions/{id}` (full-page navigation), while clicking
   SHIPMENT_INQUIRY / INVOICE_QUERY / COMPLAINT rows updated the
   right-pane detail in place. Same row affordance, two different
   outcomes — operator could not predict the page transition until
   it happened.
2. **Missing return path.** The deep-link jumped to a detail page
   whose breadcrumb only said "Back to Queue", taking the operator
   to `/exceptions` instead of back to `/inbox`. (Addressed by the
   `?from=inbox` referrer change; documented here for completeness.)
3. **Implicit navigation is user-hostile.** The operator's stated
   preference is to *browse* the inbox and choose when to leave it.
   A row click that silently navigates off the surface violates the
   browse-inbound mental model the inbox is supposed to provide.

### Updated decision

* **Every `/inbox` row click selects locally.** The dispatch in
  `src/app/inbox/page.tsx::handleActivate` no longer branches on
  `item.exception_id`; all rows invoke `setSelectedId(item.id)`
  unconditionally. Master-detail UX is now consistent across every
  inbox category.
* **The right-pane detail renders an explicit jump button** when
  `selected.exception_id` is present. The button is labelled
  "Open in Exception Queue" and pushes
  `/exceptions/{exception_id}?from=inbox`. The operator chooses
  when to leave the inbox surface; navigation is no longer a side
  effect of clicking a row.
* **`?from=inbox` is whitelisted** in
  `src/app/exceptions/[id]/page.tsx::BACK_TARGETS` so the detail
  page renders "Back to Inbox" instead of the default
  "Back to Queue". The bidirectional navigation requirement from §6
  (3) is preserved.
* **Exception Queue → inbox back-link is unchanged** (`event.metadata.source_email_id`
  → `/inbox?msg={id}`).

### What stays from §6

* The unified *detail surface* itself — `<EmailSourceSection>` above
  `<EmailOrderEntrySection>`, audit-bearing rows, no per-intent
  page-level dispatch — is unchanged. The operator still sees the
  same source-email + agent-recommendation + resolution-actions
  page when they choose to navigate.
* The compliance posture is unchanged. The audit-bearing fields on
  the detail surface are unaffected by where the operator clicked
  to get there.
* Phase F (inbox productisation under proposed ADR-036) remains
  the right home for the upstream `email-intelligence-agent`
  integration and the per-classification surface; this supersession
  is purely a UX-layer change in front of the existing Phase G data
  contract.

### Why now

The Phase G.3 deep-link was a one-click-to-detail optimisation
predicated on the assumption that operators always want the detail
page when they click a row. The operator's actual workflow — browse,
triage, decide where to dive in — does not match that assumption.
A consistent master-detail UX with an explicit jump button costs
one extra click for the cases the operator chooses to act on, and
zero clicks (with much better predictability) for the cases they
choose to skim. The trade-off lands on the operator's side.

### Tests guarding this decision

* `asoe-ui/tests/contract/test_navigation_chrome.test.ts` — file-scan
  assertions that the inbox handler invokes `setSelectedId` for
  every row (no `router.push` in the row dispatch) AND that the
  right-pane detail renders an "Open in Exception Queue" button
  with `?from=inbox` when the selected item has an `exception_id`.
* `asoe-ui/tests/browser/inbox-navigation-chrome.spec.ts::N1` —
  end-to-end: row click stays on `/inbox`, right pane updates,
  jump button click navigates to `/exceptions/{id}?from=inbox`,
  Back button returns to `/inbox`.

If a future change reverts to the deep-link dispatch, both tests
fail and force a rationale + this ADR sub-section to be revised
in the same PR.

---

## 6.2 Amendment (2026-05-12) — `event_type` rename is **transitional**, with a deadline

The 2026-05-09 vocabulary work (commit `dcff7e4`,
`refactor(intent): rename EMAIL_ORDER_ENTRY → MANUAL_ORDER_INTAKE
(§3.2)`) renamed the intent value end-to-end. The corresponding
event-type literal — `event_type="EMAIL_ORDER_ENTRY_REQUEST"` —
was deliberately **not** renamed in the same sweep because
upstream producers (the real EDI / email-intake systems and the
sandbox `StubGateway` emitters) cannot be cut over in a single
commit. The 2026-05-11 gap analysis flagged this as the
most-touched stale name in the codebase (gap report row C3); the
gap-remediation rollout plan deferred the binding decision to
this S13 amendment.

### Binding decision

`event_type="EMAIL_ORDER_ENTRY_REQUEST"` is **transitional**, not
permanent. The canonical name post-cutover is
`event_type="MANUAL_ORDER_INTAKE"` — channel-neutral, in
line with the rest of the ADR-038 vocabulary work.

Three rules govern the transition:

1. **Dual acceptance** — `api/case_resolver.py::_MANUAL_EVENT_TYPES`,
   `constraints/fallback_backend.py`, and
   `agents/harness.py::ROUTABLE_EVENT_TYPES` accept **both**
   `EMAIL_ORDER_ENTRY_REQUEST` and `MANUAL_ORDER_INTAKE`
   during the transition window. Routing decisions (case-agent
   dispatch, fallback intent classification, sandbox stub
   emission) treat the two as equivalent.
2. **Producer migration** — every producer (sandbox stub gateway,
   real EDI ingestion, `email-intelligence-agent` integration when
   it lands per Phase F) switches to emitting
   `MANUAL_ORDER_INTAKE` before the deadline. The
   canonical name on new code paths is the new one; the old name
   stays as a read-side compatibility shim only.
3. **Hard cutover deadline — 2026-08-12** (three months from this
   amendment). On or after that date, an inbound event carrying
   `event_type="EMAIL_ORDER_ENTRY_REQUEST"` is rejected by the
   API with a `400 Bad Request` payload citing this section. The
   deadline gives downstream integrators a full quarter to update
   their producers; the §28.6 Grafana dashboard tracks the
   declining-trend metric described below so the deadline is
   data-driven rather than guesswork.

### Why transitional (not permanent)

The "permanent legacy alias" alternative was considered and
rejected. Three reasons:

* **Audit-trail clarity.** Hash-chained `policy_audit_log` rows
  (ADR-023) carry the event_type verbatim. A permanent dual-name
  state means audit queries `WHERE event_type = 'MANUAL_ORDER_INTAKE'`
  silently miss half the relevant rows. The right answer is to
  cut producers over once; the rows written before the cutover
  are grandfathered (existing rows do not change).
* **Codebase signal.** The 2026-05-09 intent rename already
  touched 200+ call sites. Leaving the event_type literal on the
  pre-rename name is a permanent grep-confusion tax for every
  future contributor — they encounter `EMAIL_ORDER_ENTRY_REQUEST`
  next to `MANUAL_ORDER_INTAKE` and have to remember that one is
  legacy.
* **Vocabulary discipline.** ADR-038 §3.2 (channel-neutral issue
  naming) makes channel-specific event names a deprecated pattern.
  `EMAIL_ORDER_ENTRY_REQUEST` is channel-specific by construction
  (says "email"). Keeping it forever directly contradicts a
  binding decision in a more recent ADR.

### What stays from the pre-amendment state

* The pre-rename rows in `policy_audit_log` continue to carry
  `event_type="EMAIL_ORDER_ENTRY_REQUEST"`. The hash chain
  (ADR-023) forbids retroactive mutation, and no migration script
  is invoked to re-label those rows. Audit queries written
  against the legacy name keep working.
* The intent itself (`MANUAL_ORDER_INTAKE`) is unchanged. Only
  the event_type literal moves; the routing target, the recipe
  (`EmailOrderEntryRecipe.py` → `ManualOrderIntakeRecipe.py`, see
  §6.3 below), the analysis adapter, and the UI section are all
  driven by the intent, which is already canonical.
* Read-side compatibility shims in `_MANUAL_EVENT_TYPES` and the
  fallback backend stay in place until the deadline, then are
  removed in the same PR that flips the hard rejection on.

### Observability — deprecation deadline is data-driven

Two Prometheus metrics track the migration:

* `deprecated_event_type_received_total{event_type="EMAIL_ORDER_ENTRY_REQUEST"}` —
  counter, incremented on every inbound event still carrying the
  legacy name. Source: `api/case_resolver.py::resolve_or_open_case`.
  The trend line goes to zero before the deadline; if it does
  not, the deadline is extended one quarter and this section
  amended (a "cutover did not complete" signal is preferable to
  surprising producers on the deadline day).
* `event_type_received_total{event_type}` — gauge, snapshot of
  inbound event_type cardinality. Surfaces both names side by
  side on the §28.6 dashboard so the migration progress is
  visible to producer teams without needing to query logs.

### Tests guarding this amendment

* `tests/contract/test_event_type_alias_compatibility.py` (S14)
  — invariant: both `EMAIL_ORDER_ENTRY_REQUEST` and
  `MANUAL_ORDER_INTAKE` resolve to the same intent /
  recipe / autonomy levels. Locks the dual-acceptance contract
  until the deadline.
* `tests/contract/test_deprecated_event_type_metric.py` (S14)
  — the deprecation counter increments on legacy name receipt,
  is not incremented on the canonical name. Forces the metric
  to stay on the dashboard.
* When the deadline passes, the dual-acceptance tests flip
  polarity (legacy name MUST 400) and the deprecation counter
  test is retired with a note linking to this section.

### Companion S14 work (rollout plan row)

The same gap-remediation rollout that ratified S13 also gates
S14 on this amendment:

* `recipes/EmailOrderEntryRecipe.py` →
  `recipes/ManualOrderIntakeRecipe.py` with a back-compat re-export
  at the old path for one release cycle (post-deadline, the stub
  is deleted).
* `contracts/policy.py::EMAIL_ORDER_ENTRY_AUTONOMY_LEVELS` →
  `MANUAL_ORDER_INTAKE_AUTONOMY_LEVELS` with a deprecation alias
  on the old name (`AUTONOMY_LEVELS_ALIAS = NEW_NAME`).
* `tests/test_e2e_email_order_entry.py` →
  `tests/test_e2e_manual_order_intake.py` (filename + docstring),
  pytest collection picks up the new name without further
  configuration.
* Producer-side updates land in S14b (sandbox stub gateway emits
  the new event_type by default; legacy emission moves behind a
  `ASOE_EMIT_LEGACY_EVENT_TYPES=1` env flag for backward-compat
  testing).

### §6.2 status

* **Status:** Accepted (2026-05-12). Supersedes the implicit
  "permanent legacy alias" posture that existed by default after
  commit `dcff7e4`.
* **Definition of Done for S14:** all three rename pairs landed,
  both contract tests green, deprecation counter wired,
  Grafana dashboard tile present. S14 PR body cites this
  section.

---

## 6.3 Recipe-file rename (S14 — companion to §6.2)

`recipes/EmailOrderEntryRecipe.py` is renamed to
`recipes/ManualOrderIntakeRecipe.py` in S14. The recipe's
`__name__`, registry key, and exported symbols are updated. A
back-compat re-export at `recipes/EmailOrderEntryRecipe.py` keeps
the old import path resolving for one release cycle; consumers
import from the new path going forward. The re-export module is
two lines plus a deprecation comment.

`recipes/registry.py` carries both keys during the transition:
`{"EmailOrderEntryRecipe.py": ManualOrderIntakeRecipe, "ManualOrderIntakeRecipe.py": ManualOrderIntakeRecipe}`.
The dual-key map is removed in the same PR that flips the §6.2
hard rejection on (post-deadline).

---

## 7. Notes for the next reviewer

* This ADR explicitly carves **out** the extraction pipeline, MCP integration, and
  calibration loop from Phase A. The recipe is a *thin*, pure scoring/classification
  function — exactly the role recipes play in this codebase.
* The "Non-Disable-able Floor" is encoded both as a `required_for_audit=True` gateway
  declaration *and* as a recipe-level hard-reject branch (defence in depth — gateway
  failure halts before the recipe; if a misconfiguration lets the run through with
  `non_disableable_floor_breached=True` on metadata, the recipe still rejects).
* The spec's 0.95 / 0.85 / 0.99 thresholds were chosen by the PO for L2 default
  behaviour. They land as platform defaults in `policy.py`. Tenant-level overrides go
  through ADR-030's config override hierarchy in a follow-up; this matches the
  DuplicatePO weights story.
