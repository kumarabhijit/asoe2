# ADR-034 — Email Order Entry Skill: Bucketed Mapping, Phase Plan, and Halt-Conditioned Deferrals

**Status:** Proposed
**Date:** 2026-05-04
**Decision driver:** Product Owner spec `docs/specs/order-entry-from-email-product-spec.md`
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
| §2 Problem Domain, Friction Points | REFERENCE | Already preserved verbatim in `docs/specs/order-entry-from-email-product-spec.md` |
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

* `asoe-ui/src/components/sections/EmailOrderEntrySection.tsx`
* Hook into the analysis composer so the section renders for `EMAIL_ORDER_ENTRY` records.
* Architectural lock test ensuring the section is registered in the section map.

### Phase D — MCP tool surface (ADR-035, separate review)

* Defines how MCP tool calls (`resolve_customer_by_email`, `simulate_sales_order`,
  `create_sales_order`, etc.) are exposed as gateway operations.
* Production-grade ERP integration is a separate platform track; current production
  remains StubGateway-backed.

### Phase E — Calibration & graduation (under ADR-032)

* The §7 graduation metrics feed the ADR-032 calibration loop. No separate ADR needed.
* L4 → L3 auto-demotion enforced in `policy.py` until calibration ships graduation
  signals.

---

## 5. Acceptance for Phase A

* `python -m pytest` is green (no failing tests).
* `pre-commit` hooks (if any) pass.
* The vocabulary sync invariants in `tests/test_constraints.py` and
  `tests/test_registry.py` continue to hold (these tests derive expectations
  dynamically from the literals/registry, so the count update is automatic).
* The new recipe contains no I/O, no LLM calls, no side effects (`grep` rule).
* The spec file is preserved at `docs/specs/order-entry-from-email-product-spec.md`,
  not under `skills/`.

---

## 6. Notes for the next reviewer

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
