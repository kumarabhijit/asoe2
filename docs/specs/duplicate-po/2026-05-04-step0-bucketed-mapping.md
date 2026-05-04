# Duplicate-PO Skill — Step 0 Bucketed Mapping

**Date:** 2026-05-04
**Source spec:** `b2b-duplicate-po-check` (full text + four reference files preserved under this directory after PR #92)
**Conversion contract:** `prompts/po-spec-to-asoe.md` Step 0 — Spec Analysis Gate
**Status:** Action-item A1 from `2026-05-03-design-review.md` — kickoff PR input
**Implementation PR:** companion to this commit (action items A1 + A4 + ADR-029 V1 minimum)

---

## Purpose

`prompts/po-spec-to-asoe.md` Step 0 requires every section of a Product
Owner specification to be classified into one of three buckets before
any code is written:

- **SKILL territory** — guides reasoning only; no execution logic
- **RECIPE territory** — deterministic execution; pure functions; no I/O
- **REFERENCE / PRODUCT-SPEC territory** — not runtime code (DDL, API
  shapes, UX, integration patterns, calibration methodology, metrics)

This document records that classification for the new
`b2b-duplicate-po-check` skill spec. Subsequent implementation work (this
PR and follow-ups A4–A10) maps directly onto the bucketed table below.

The classification was already implicit in ADRs 028–033, but
`po-spec-to-asoe.md` requires it as an explicit, written input to the
implementation PR. This file is that input.

---

## Bucketed mapping

| Spec section | Content | Bucket | Lands in (current artifact) | Status post-PR-92 |
|---|---|---|---|---|
| §1 Overview / Trigger | EDI 850 duplicate detection use case; trigger event type | **SKILL** | `skills/duplicate-po_SKILL.md` §1 | Already in main; light touch this PR (mention new gateway dep) |
| §2 Reasoning protocol | Numbered reasoning loop; recipe selection guidance; "do not improvise the logic" | **SKILL** | `skills/duplicate-po_SKILL.md` §2 | Already in main; touch only if §1 changes |
| §3 Detection algorithm — 8 signals + weights | `po_number 0.30, customer_id 0.15, line_items 0.20, amount 0.10, timestamp 0.10, ship_to 0.05, channel 0.05, delivery_date 0.05` | **RECIPE** | `recipes/DuplicatePORecipe.py::_WEIGHTS` (platform default) | Already in main; weights become **overridable per ADR-029** in this PR |
| §3 Detection algorithm — thresholds | `auto_block 0.90 / review_required 0.70 / soft_flag 0.50` | **RECIPE** (parameters injected by orchestration) | `recipes/DuplicatePORecipe.py::detect_duplicate_po(threshold_*)` | Already in main; injected by `validate_types` from `contracts/policy.py` |
| §3 Detection algorithm — decision tree | `(classification, original_fulfilled, has_revision_indicator, line_items_identical) → action` | **RECIPE** | `recipes/DuplicatePORecipe.py::_resolve_action` | Already in main |
| §4 Resolution actions | `BLOCK_AND_NOTIFY / MERGE / SUPERSEDE / ALLOW_BOTH / ESCALATE / REQUEST_BUYER_CONFIRMATION` | **RECIPE output** under `constraints/specs.py::AllowedResolutionAction` | `constraints/specs.py` Literal | Already in main; constrained-generation regex auto-derives via `_literal_to_regex` |
| §4 Autonomy levels | L1–L4 per resolution action | **RECIPE** (parameters injected from policy) | `recipes/DuplicatePORecipe.py::detect_duplicate_po(autonomy_levels=...)` + `contracts/policy.py` mapping | Already in main; injected by `validate_types` |
| §5 Notifications | `duplicate_po_blocked / duplicate_po_amended / duplicate_po_inquiry` | **RECIPE output** + **gateway effect** | `_NOTIFICATION_TEMPLATES` in recipe + `GatewayEffect(buyer_notification.send)` in `recipes/registry.py` | Already in main |
| §6 API surfaces | `POST /v1/duplicate-check`, `GET /v1/exceptions/duplicates/:id` envelope, etc. | **REFERENCE** (product-spec); the canonical envelope is the **API team's deliverable** per ADR-028 G2 | Reference: `docs/specs/duplicate-po/api-examples.md` (preserved as REFERENCE header). Implementation: `api/routes/duplicates.py` + `api/analysis_composer.py` extension | **Deferred to A6** — sprint following kickoff |
| §7 Database schema (per-intent tables) | `incoming_po`, `incoming_po_lines`, `duplicate_check_results`, `duplicate_check_audit` | **REFERENCE** (product-spec); ADR-028 **rejects** per-intent tables for V1 in favor of unified ASOE exception lifecycle | Reference: `docs/specs/duplicate-po/schema.sql` (preserved with REFERENCE header pointing to ADR-028 mapping) | Decision encoded in **ADR-028**; unified lifecycle in `db/repository.py` already serves this |
| §7 Metadata contract for `OrderEvent.metadata` and `recipe_output` | What JSONB keys are allowed for DUPLICATE_PO | **REFERENCE artifact** (this PR — documented contract); **write-time enforcement is A5** | New: `docs/specs/duplicate-po/metadata-contract.md` (this PR, ADR-028 G1 minimal) | A5 deferred to next sprint |
| §8 Configuration hierarchy (5-level) | platform → tenant → tier → customer → channel; partial-override merge semantics | **RECIPE input** resolved by **gateway**; merge policy in **ADR-029**; storage shape in **ADR-030** | Recipe accepts `weights=...` (this PR); gateway: `gateways/tenant_config.py` (this PR — file-backed V1 per ADR-029); full table+API: A9 | Partial in this PR (V1 minimum per ADR-029); full A9 deferred to first 2 sprints |
| §8 ConfigChange domain event | Every config write audited via hash chain | **REFERENCE** today (artifact-only this PR); full implementation is A9 | ADR-030 §G | Deferred to A9 |
| §9 Calibration methodology | Logistic regression weight calibration loop | **REFERENCE** (deferred per ADR-032; preserved verbatim under FUTURE header) | `docs/specs/duplicate-po/calibration-methodology.md` | Deferred entirely (ADR-032); this PR ships the supporting surfaces (overridable weights, reason codes) |
| §10 Override reason codes (8) | `INTENTIONAL_REORDER, AMENDED_PO, BLANKET_RELEASE, SYSTEM_RETRY_VALID, DIFFERENT_SHIP_TO, CONFIRMED_DUPLICATE, PARTIAL_OVERLAP, OTHER` | **CONSTRAINT vocabulary** | `constraints/specs.py::INTENT_REASON_TAGS["DUPLICATE_PO"]` (this PR per ADR-033) | This PR (A4) |
| §10 UI 3-cluster grouping | "Confirm the agent" / "Override with structured reason" / "Edge case" | **REFERENCE** for this repo; **UI work in asoe-ui** | `asoe-ui/src/app/exceptions/OverrideChooserDialog.tsx` follow-up PR | A8 — separate UI PR after this lands |
| §11 Tech stack / dependencies | Postgres / Pinecone / SAP MCP | **REFERENCE** (product-spec) | Implicitly satisfied by existing ASOE infrastructure | n/a |
| §12 Testing strategy & metrics | precision/recall targets, SLA budgets | **REFERENCE** (product-spec); the runtime contracts are the per-test assertions in `tests/` | `tests/test_recipes.py::TestDuplicatePORecipe` (existing + new this PR) | Per-ADR test additions in this PR; metrics dashboard is A10 (V1.5) |

---

## What this PR delivers (summary)

Per the table above, this kickoff PR (action items A1 + A4 + ADR-029 V1
minimum) covers:

1. **Step 0 mapping** — this file.
2. **§10 reason codes** — `INTENT_REASON_TAGS["DUPLICATE_PO"]` curated to the
   8 codes (ADR-033). `AllowedOverrideReasonTag` Literal expanded to the
   union of legacy lowercase codes + 8 new SCREAMING_SNAKE_CASE codes
   (per ADR-033 V1 §1: per-intent narrowing happens at the API surface,
   not the type level).
3. **§3 weights overridability** — `detect_duplicate_po` gains an
   optional `weights` parameter; module-load assertion preserved on
   `_WEIGHTS` (platform default), runtime `_assert_weight_contract` with
   `1e-4` tolerance per ADR-029. New `WeightContractViolation` exception.
4. **§8 config resolver (V1 minimum)** — `gateways/tenant_config.py`
   reads `gateways/configs/duplicate_po/defaults.json`, applies the
   layered merge per ADR-029 (platform → tenant → tier → customer →
   channel), returns the validated weight map plus per-layer
   contribution trace. Fail-closed-to-platform on
   `WeightContractViolation` with audit-chain entry.
5. **Wiring** — `tenant_config` registered as a `GatewayDependency` on
   `DuplicatePORecipe.py`. `orchestration/nodes.py::validate_types`
   extracts `weights` from `state.resolved_data["tenant_config"]` and
   passes it into the recipe.
6. **§7 metadata contract artifact (ADR-028 G1 minimal)** — written
   contract for `OrderEvent.metadata` and `ExecutionLog.outputs` for
   DUPLICATE_PO. Write-time enforcement (A5) is a follow-up sprint.
7. **Skill update** — `skills/duplicate-po_SKILL.md` reasoning loop notes
   the new `tenant_config` dependency that resolves before recipe
   invocation.
8. **Tests** — vocabulary-sync, weight override happy/edge paths,
   gateway merge order + per-layer trace, fall-back-to-platform path.

---

## What this PR explicitly does NOT deliver

| Item | Source | Owner | Sprint |
|---|---|---|---|
| Canonical `GET /api/v1/exceptions/duplicates/:id` envelope + composer | ADR-028 G2 / A6 | API team | Sprint following kickoff |
| Write-time metadata-contract validation | ADR-028 G1 / A5 | Backend | Sprint following kickoff |
| `tenant_id` CI gate in `db/repository.py` | ADR-028 G4 / A7 | Platform | Sprint following kickoff |
| Full `tenant_config` table + V006 migration + 5 API endpoints + health endpoint | ADR-030 / A9 | Backend | First two sprints post-kickoff |
| `ConfigChange` discriminated-union domain event + audit-chain wiring | ADR-030 §G / A9 | Backend | First two sprints post-kickoff |
| `OverrideChooserDialog` 3-cluster UI for `DUPLICATE_PO` | ADR-033 §D / A8 | Frontend | Separate PR on `asoe-ui` after this PR lands |
| Manager override-pattern dashboard | ADR-033 / A10 | Frontend + analytics | V1.5 |
| `config_validation_alert` UI surface (admin config UI) | ADR-029 V1 §7 (UI half) | Frontend | V1.5 (alongside A9) |
| Calibration loop, training pipeline, calibration service | ADR-032 | Deferred | Out of scope until ADR-032 re-opened |
| Read-projection split (MV first, table later) | ADR-031 | Conditional | Triggered by T1–T5 in ADR-031 |
| Drop-ship preset re-evaluation (`line_items: 0.10`) | ADR-029 Notes / O5 | Product | Non-blocking; admin can override per customer |
| Bulk export of resolved exceptions for QBR | Item 7 / O2 | Backlog | Backlog |

---

## Halt conditions checked

`po-spec-to-asoe.md` Step 0 lists six halt conditions. Status against
each, post-PR-92:

1. **External I/O in recipe?** No — gateway dependencies (OMS fulfillment,
   matched-PO details, tenant_config) resolve facts; recipe applies rules.
2. **Multiple autonomy tiers / unclear shadow mapping?** No — autonomy
   levels are per resolution action, injected from `contracts/policy.py`
   and consumed by `execute_recipe` for L1/L2 → MANUAL_REVIEW_REQUIRED
   routing.
3. **Multi-step workflow spanning intents?** No — `DUPLICATE_PO` is a
   single intent; the workflow runner is not involved.
4. **New `GraphState` field that doesn't fit `OrderEvent.metadata`?** No —
   `signal_scores` and `matched_po_id` already declared as
   `expected_metadata_keys` on the recipe spec; resolved facts flow
   through `state.resolved_data` (existing field).
5. **Feedback / retraining loop?** Calibration loop is out of scope per
   ADR-032; this PR ships only the supporting data-collection surfaces.
6. **Threshold/weight ambiguity (fixed vs configurable)?** Resolved by
   ADR-029 (weights are tenant-configurable; recipe receives a validated
   resolved map; no silent renormalization).

No halt conditions trigger. Proceeding with implementation.

---

## References

- `prompts/po-spec-to-asoe.md` — conversion contract (Step 0 gate)
- `docs/specs/duplicate-po/2026-05-03-design-review.md` — design-review
  minutes (action items A1–A10)
- `docs/specs/duplicate-po/2026-05-10-adr-review.md` — sign-off review
  (binding revisions)
- `docs/adr/ADR-028..033` — settled architectural decisions
- `docs/specs/duplicate-po/{schema.sql, config-defaults.json, calibration-methodology.md, api-examples.md}`
  — preserved reference materials (REFERENCE / FUTURE headers)
