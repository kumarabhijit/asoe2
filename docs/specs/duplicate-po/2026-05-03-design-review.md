# Duplicate-PO Architecture Design Review — Minutes

**Date:** 2026-05-03
**Format:** Virtual design-review session, structured round-table
**Output:** Six ADRs (028–033) + this minutes record
**Facilitator:** Architecture review chair

> **Note on attendance.** Participants below are stakeholder *archetypes* representing the schools of thought / roles that needed to weigh in on these decisions. Quotes are reconstructed from the round-table format used in session, not verbatim transcripts. The decisions, conflicts, and action items captured here are binding architectural commitments and are reflected in ADR-028 through ADR-033.

---

## Attendees

| Tag | Role | Concern owned |
|---|---|---|
| E1 | DDD / Bounded-Context Architect | Aggregate integrity, model honesty |
| E2 | Data-Intensive Systems Architect (Kleppmann lens) | Read-path performance, query patterns |
| E3 | Event-Sourcing / CQRS Architect | Write-model purity, projections |
| E4 | Multi-Tenant SaaS / RLS Architect | Tenant isolation, config-resolution paths |
| E5 | SOX / Compliance Lead | Audit trail completeness, immutability |
| E6 | ML / Feature-Store Engineer | Training-data extractability for future calibration |
| E7 | B2B / ERP Integration Veteran | Domain edge cases, amendment / cross-subsidiary patterns |
| U1 | CS Associate (L2-resolver, primary UI user) | Queue speed, override friction, recommendation clarity |
| U2 | CS Manager (team lead) | SLA, override patterns, autonomy tuning |
| U3 | Tenant Admin (IT / ops owner of config) | 5-level config control, sandbox, audit of config changes |
| F | Facilitator (chair) | Forces decisions, parks scope creep |

---

## Pre-read

1. New `b2b-duplicate-po-check` skill (full text + four reference files: `schema.sql`, `config-defaults.json`, `calibration-methodology.md`, `api-examples.md`) — all preserved under `docs/specs/duplicate-po/`.
2. `prompts/po-spec-to-asoe.md` conversion contract.
3. Current ASOE state: `skills/duplicate-po_SKILL.md`, `recipes/DuplicatePORecipe.py`, `recipes/registry.py`, `constraints/specs.py`, `contracts/policy.py`, `skills/loader.py`, `orchestration/nodes.py`.
4. Prior gap analysis (this session's input).
5. Confirmed direction: **calibration deferred** (PO/customers supply calibrated values via config); recommended unified-lifecycle storage with three guard-rails (later expanded to four during the session).
6. Two open architect decisions: **D1** (storage shape), **D2** (override-merge policy).

---

## Agenda

1. D1 — Storage shape (unified lifecycle vs per-intent tables)
2. Canonical read API for duplicate-PO state
3. 5-level override hierarchy: V1 scope
4. D2 — Override merge / renormalization
5. Override reason-code vocabulary & UX
6. Read-projection split trigger (when to revisit D1)
7. End-user surfaces — adequacy review
8. ADRs to produce, action items

---

## Item 1 — D1: Unified lifecycle vs per-intent tables

**E3 (CQRS):** ASOE is already event-sourced via `OrderEvent` → `ExecutionLog` → audit hash chain. Adding `incoming_po`, `incoming_po_lines`, `duplicate_check_results`, `duplicate_check_audit` as a parallel write model invites state drift. Strong support for unified write.

**E1 (DDD):** Conditional support. The unified row is honest only if `Exception` has invariants you can write down without saying "depends on intent." Otherwise it's a pattern-of-convenience and `metadata` JSONB will become a junk drawer within two intents.

**E2 (Kleppmann):** Show me the top-10 queries first. For V1 with current volume, unified plus a couple of GIN indexes on JSONB keys is fine.

**E6 (ML):** Will accept unified for V1 only with a written commitment to a read projection before calibration work starts. Pulling labeled training pairs out of nested JSONB across 10M rows is a known pain.

**E7 (ERP veteran):** Both shapes ship in production. SAP went per-document-type; HighRadius/BlackLine went unified. Don't relitigate — pick one and spend the saved effort on amendment-PO and cross-subsidiary modeling.

**E5 (SOX):** Storage shape is your problem. Mine is whether the audit hash chain provably covers every state mutation including the JSONB fields. Demonstrate that and I'm done.

**E4 (Multi-tenant):** Indifferent. But whatever you pick, RLS or query-level enforcement of `tenant_id` must be in every read path. Want a CI check that fails if a `repository.py` query is missing tenant filtering.

**F (forcing the decision).** Hands?

→ **Decision D1: Adopt unified lifecycle for V1**, with four guard-rails locked as binding (Items 2, 6, plus a new explicit-contract guard-rail per E1, plus the SOX coverage proof per E5).

E1's caveat formalized: add a fourth guard-rail — **a documented schema for what `OrderEvent.metadata` and `ExecutionLog.recipe_output` may contain for `DUPLICATE_PO`**, enforced at write-time. JSONB junk-drawer risk is real and cheap to mitigate now.

→ **Captured as ADR-028.**

---

## Item 2 — Canonical read API for duplicate-PO state

**E5 (SOX):** Non-negotiable. Need one endpoint that returns the entire reconstructable record — incoming PO data, matched PO, all signals, all audit events, all human actions, in chronological order.

**U1 (CSR):** Same on the UI side. Layer-2 deep-dive needs one fetch, not five. Today the panel sometimes flickers as data comes in piecewise.

**U2 (Manager):** Bulk export of resolved exceptions for the past quarter, with all the same fields, for QBR reports.

**E3 (CQRS):** This is your read model. Build it as a view or thin adapter over the unified write side. Don't let it leak back into the write path.

→ **Decision:** Define `GET /api/v1/exceptions/duplicates/:id` to return a canonical envelope: `{incoming_po, matched_po, signal_breakdown, classification, recommended_action, autonomy_level, agent_reasoning, audit_trail[], human_actions[]}`. Implemented as a composer in `api/analysis_composer.py` family — single round-trip, single source of truth, no UI-side stitching. Bulk-export endpoint as a follow-up ticket, not blocker.

→ **Captured as ADR-028 Guard-rail 2.**

---

## Item 3 — 5-level config override hierarchy: V1 scope

**U3 (Admin):** This is *my* surface. Need `platform → tenant → customer-tier → customer-specific → customer-channel`, with sandbox-vs-production separation, and every config change audited with who/when/before/after. Sandbox testing before production push is non-negotiable.

**E4 (Multi-tenant):** Resolution path needs to be deterministic and observable. When duplicate-PO fires for `(tenant=acme, customer=walmart, channel=EDI)`, I should be able to ask "which 5 layers contributed and what was the merged result?" and get a trace.

**U2 (Manager):** Want to tune autonomy levels per-customer without filing a ticket. If Walmart's L4 gets a buyer dispute, downgrade to L3 the same day.

**E6 (ML):** Eventually this is also where calibrated weights land. Make sure the schema can hold the full `score_weights` map, not just per-tier presets.

**E1 (DDD):** What is the *aggregate* here — `TenantConfig`, or `CustomerConfig`, or a single `DetectionPolicy`? Get the model right before storage.

**E7 (ERP veteran):** In real deployments, customer-channel overrides are how you handle "Walmart EDI is reliable, Walmart Portal isn't." Don't ship without level 5.

**F:** Scope question — full 5-level in V1, or 3-level (platform/tenant/customer) in V1 with 4 and 5 in V1.5?

**U3:** Will live with 3-level for V1 *only if* the data model and resolution function are written for 5 from day one, with empty levels 4 and 5 as no-ops.

→ **Decision:** Resolution function and storage written for 5 levels, V1 ships with level-4 and level-5 inputs accepted but no UI to populate them — admins set via API. UI for customer-specific and customer-channel config in V1.5. Every resolution emits a trace with which layer contributed which value.

→ **Captured as ADR-030.**

---

## Item 4 — D2: Override merge / renormalization policy

Three options floated: (a) customer config supplies all 8 weights, fill-defaults + assert sum=1; (b) customer config supplies partial weights, engine renormalizes proportionally; (c) customer config supplies partial weights, engine fills missing keys from the next-higher level in the hierarchy.

**E1 (DDD):** (c) is the only one honest with the hierarchy you just defined. (a) collapses inheritance; (b) creates surprising rebalancing the customer didn't ask for.

**E6 (ML):** (c) also matches how calibrated weights will arrive — the customer will provide a delta from their tier baseline, not the full vector.

**U3 (Admin):** Want to write `{po_number: 0.10, line_items: 0.35}` and have the system do the right thing.

**E2 (Kleppmann):** Whatever you pick, the runtime cost is identical. Pick on semantics.

**F:** Concern — option (c) means a partial customer override could still produce a non-1.0 sum if it doesn't perfectly offset what it's overriding.

**E1:** Yes. Validate sum-to-1 with `1e-6` tolerance after merging the full hierarchy. If it fails, fail closed: log error, fall back to platform default, surface in admin config UI as a validation error.

→ **Decision D2:** Merge by walking the hierarchy from top to bottom, layering partial weight maps on top of inherited values. After full resolution, assert `sum(weights) == 1.0 ± 1e-6`. If violated, fail closed to platform defaults and emit a config-validation alert. The recipe receives the resolved-and-validated weight map; it does not renormalize.

→ **Captured as ADR-029.**

---

## Item 5 — Override reason-code vocabulary & UX

**E6 (ML):** Need structured reason codes — they're the labels for the future calibration loop. Free-text alone is unusable for training. The 8 codes from the calibration doc are good: `INTENTIONAL_REORDER`, `AMENDED_PO`, `BLANKET_RELEASE`, `SYSTEM_RETRY_VALID`, `DIFFERENT_SHIP_TO`, `CONFIRMED_DUPLICATE`, `PARTIAL_OVERLAP`, `OTHER`.

**U1 (CSR):** Eight is too many on a hot queue. Will click the first one that fits or always pick `OTHER` to get past the modal. Group them — "agent-was-wrong" vs "agent-was-right-but-business-decision" vs "edge-case."

**E5 (SOX):** Free-text is fine *in addition* to structured codes — the audit narrative often lives there. Don't drop it.

**U2 (Manager):** Want a dashboard of override-reason distribution by customer and analyst. That's the early-warning signal for drift.

**E1 (DDD):** These are domain concepts — they belong in `constraints/specs.py:INTENT_REASON_TAGS['DUPLICATE_PO']`, not as magic strings in UI.

→ **Decision:** Adopt the 8 codes verbatim in `constraints/specs.py`. UI groups them into 3 visual clusters in `OverrideChooserDialog`. Free-text notes remain mandatory only when reason is `OTHER`, optional otherwise. Manager dashboard view added in V1.5.

→ **Captured as ADR-033.**

---

## Item 6 — Read-projection split trigger

**E2 (Kleppmann) + E6 (ML) jointly:** Don't leave "maybe we'll split later" in the air. Write the trigger now.

→ **Decision:** Splitting `duplicate_check_results` into a dedicated read projection (materialized view first; physical table only if MV is insufficient) is triggered when **any** of the following hold for two consecutive weeks:

- P95 latency on `GET /api/v1/exceptions/duplicates` > 800 ms (T1)
- P95 latency on `GET /api/v1/exceptions/duplicates/:id` > 400 ms (T2)
- Duplicate-PO query share > 30% of total exception-route DB time (T3)
- More than 2 GIN indexes required on `ExecutionLog.recipe_output` to keep T1/T2 within budget (T4)
- Calibration work scheduled within the next 90 days (T5 — proactive, from E6's lens)

This goes into the ADR. Revisiting D1 outside these triggers is out of scope.

→ **Captured as ADR-031.**

---

## Item 7 — End-user surface adequacy

**U1 (CSR):**
- Queue load needs to be ≤500 ms with 200+ open exceptions
- Confidence as visual bar, not %; differences in side-by-side highlighted in amber as spec says
- Approve / Override / Escalate must be keyboard-shortcut-able for power users
- Layer-2 details should not auto-expand — they're cognitive overhead 90% of the time

**U2 (Manager):**
- Real-time view of team's open queue with SLA timer per row
- Override-pattern dashboard (top reasons by analyst, by customer)
- Ability to re-route flagged exceptions to a specific analyst
- Visibility into autonomy-level distribution per customer (am I running too many at L4?)

**U3 (Admin):**
- 5-level config UI (V1.5 for levels 4–5; admins use API in V1)
- Sandbox-vs-prod separation with diff-and-promote workflow
- Audit log of every config change: who, when, before, after, level
- Health endpoint that exposes resolved config for a sample `(tenant, customer, channel)` so admins can verify their override landed correctly

**E5 (SOX):** Config-change audit is non-negotiable for me too. Every change to weights or thresholds is a SOX-relevant event.

**E1 (DDD):** Treat `ConfigChange` as a domain event with its own model — not just a row in a generic 'audit_log' table.

→ **Decision:** UI gaps captured as a backlog. Config-change audit is in V1 scope (not deferrable). All UI items above logged as tickets; the keyboard-shortcut and sandbox-promote workflow are flagged as "near-term, not blockers."

→ **Captured as ADR-030 Section G (`ConfigChange` domain event); UI work tracked in `asoe-ui` backlog.**

---

## Item 8 — Conflicts surfaced and resolved

| # | Conflict | Resolution |
|---|---|---|
| C1 | E1 (DDD purity) vs E3 (unified write model) | Unified write + explicit metadata schema (E1's win on contract, E3's win on storage shape) — ADR-028 Guard-rail 1 |
| C2 | E6 (ML wants typed columns) vs E3 (CQRS purity) | Read projection on the *trigger conditions* in Item 6, not preemptively — ADR-031 |
| C3 | U1 (CSR wants 1-click override) vs E6 (ML wants 8 reason codes) | 3-cluster grouping in UI + free-text optional except for `OTHER` — ADR-033 §D |
| C4 | U2 (Manager wants per-customer autonomy tuning) vs U3 (Admin wants centralized config) | Manager actions become *proposals* into the customer-specific config layer; Admin owns merge/approve. Not a blocker for V1 — V1.5 backlog. |
| C5 | U3 (Admin wants flexibility) vs E5 (SOX wants every change audited) | Both win — full audit on every config write, no exceptions, no admin override of the audit — ADR-030 §G |
| C6 | E7 (don't relitigate storage) vs E1 (don't compromise the model) | Item 1 decision honors both — pick unified, but pay the small price for the metadata contract — ADR-028 |

---

## Decisions made (binding)

1. **D1 — Storage:** Unified ASOE exception lifecycle. Per-intent tables off the table for V1. → **ADR-028**
2. **D1 guard-rail 1:** Documented contract for `OrderEvent.metadata` and `ExecutionLog.recipe_output` for `DUPLICATE_PO`, enforced at write-time. → **ADR-028 Guard-rail 1**
3. **D1 guard-rail 2:** Canonical `GET /api/v1/exceptions/duplicates/:id` envelope. → **ADR-028 Guard-rail 2**
4. **D1 guard-rail 3:** Pre-committed split-trigger conditions. → **ADR-031**
5. **D1 guard-rail 4:** SOX hash-chain coverage proof — automated test that mutating any JSONB field without a chain entry fails. Tenant-isolation CI gate. → **ADR-028 Guard-rail 4**
6. **D2 — Override merge:** Hierarchical layered merge, validate sum-to-1 with `1e-6` tolerance, fail closed to platform default on violation. → **ADR-029**
7. **5-level override hierarchy:** Storage and resolver written for 5 levels in V1; UI for levels 4–5 in V1.5; resolution emits per-layer contribution trace. → **ADR-030**
8. **Override reason codes:** 8 codes adopted verbatim in `INTENT_REASON_TAGS['DUPLICATE_PO']`; UI groups into 3 clusters; free-text mandatory only on `OTHER`. → **ADR-033**
9. **Calibration:** Confirmed deferred. `calibration-methodology.md` archived as forward-looking spec under `docs/specs/duplicate-po/`. Read-projection trigger explicitly includes "calibration work scheduled" as a proactive condition. → **ADR-032**
10. **Config-change audit:** Mandatory in V1, modeled as a first-class `ConfigChange` domain event. → **ADR-030 §G**
11. **Tenant isolation:** CI check in `db/repository.py` to fail builds on missing `tenant_id` filtering. → **ADR-028 Guard-rail 4**

---

## ADRs produced

| ADR | Title | Status |
|---|---|---|
| 028 | Duplicate-PO Storage Shape — Unified Exception Lifecycle with Four Guard-Rails | Accepted |
| 029 | Override Merge & Renormalization Policy for Detection Weights | Accepted |
| 030 | 5-Level Config Override Hierarchy & Resolution Semantics | Accepted |
| 031 | Read-Projection Split-Trigger Conditions for Duplicate-PO | Accepted |
| 032 | Calibration Deferral and Future-State Contract | Accepted |
| 033 | Override Reason-Code Vocabulary Lifecycle (Per-Intent Curation) | Accepted |

---

## Open / parked items (explicit non-decisions)

- **O1:** Whether the eventual calibration loop runs in-process or as a separate service. Out of scope until ADR-032 is re-opened.
- **O2:** Bulk export of resolved exceptions for QBR. Backlog ticket; not a blocker.
- **O3:** Manager → admin config-change proposal workflow (C4). V1.5.
- **O4:** Customer order history (last 10) in Layer-2 detail. Backlog.
- **O5:** Drop-ship preset weights (`line_items: 0.10`) look counter-intuitive vs ERP-veteran reviewer's expectation. Flagged for product-owner review; non-blocking — admin can override.

---

## Action items

| # | Action | Owner | Due | Tracks to |
|---|---|---|---|---|
| A1 | Run `prompts/po-spec-to-asoe.md` Step 0 against the new Skill.md; produce explicit bucketed mapping + minimal-diff PR draft | Architecture chair | Week of 2026-05-04 | Implementation kickoff |
| A2 | ADRs 028–033 drafted | Architecture chair | 2026-05-03 | **Done — this PR** |
| A3 | ADR review meeting (sign-off, no changes ad-hoc — comments on the ADR drafts only) | Same group | Week of 2026-05-10 | Implementation kickoff |
| A4 | Wire `INTENT_REASON_TAGS['DUPLICATE_PO']` with the 8 codes; vocabulary-sync tests | Backend | Week of 2026-05-10 | ADR-033 |
| A5 | Define metadata-contract for `DUPLICATE_PO` in `OrderEvent.metadata` and `ExecutionLog.recipe_output`; add write-time validation | Backend | Sprint following kickoff | ADR-028 |
| A6 | Define canonical `GET /exceptions/duplicates/:id` envelope; implement composer | API team | Sprint following kickoff | ADR-028 |
| A7 | Add `tenant_id` CI check to `db/repository.py` | Platform | Sprint following kickoff | ADR-028 |
| A8 | UI work: 3-cluster reason grouping in `OverrideChooserDialog`; confidence-bar audit; amber-diff verification in `OrderComparisonSection` | Frontend | Sprint following kickoff | ADR-033, design-review Item 7 |
| A9 | `tenant_config` gateway: schema (V006 migration), resolver, ConfigChange events, 5 API endpoints, health endpoint | Backend | First two sprints post-kickoff | ADR-029, ADR-030 |
| A10 | Manager override-pattern dashboard | Frontend + analytics | V1.5 | ADR-033, Item 7 |

---

## Follow-up sessions

- **+1 week:** ADR review (028–033) — same group, focused review only on the written ADRs. Comments on ADR drafts, no ad-hoc relitigation.
- **+2 weeks:** Implementation kickoff with backend + frontend leads.
- **+1 quarter:** Calibration scoping session (E6 leads, others optional) — purely to revalidate the deferral decision and check the read-projection trigger conditions.

---

**Meeting closed.** Recording, decisions, and action items circulated within 24 hours via this minutes record. Dissent on any decision should be raised in writing on the corresponding ADR draft, not relitigated ad-hoc.
