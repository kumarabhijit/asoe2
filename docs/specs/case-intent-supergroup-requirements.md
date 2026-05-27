# Case & Intent Super-Group — Requirements

> **Status:** READY FOR PO SIGN-OFF
> **Owner:** Product (PO) + Order Management business lead
> **Replaces:** the draft of 27-May 2026 (same path, prior commit)
> **Implementation:** does **not** start until §11 sign-off block is completed.

---

## 1. Purpose

Unify ASOE2's two case-intake paths — customer email and SAP block — under a single hierarchical taxonomy: every case carries one **Intent Super-Group** (case-level classification) and one or more **Intent** leaves (granular routing key). This requirements document is the single source of truth for the data model, lifecycle, governance, and acceptance criteria of that taxonomy.

---

## 2. Scope

**In scope**

- Case data model on `OrderCase` and its child cases (currently `ExceptionRecord`).
- Intent Super-Group and Intent taxonomy storage, governance, and lifecycle.
- Two intake origins: customer-initiated (**CUSTOMER**) and SAP-pushed (**API**).
- Routing, SLA, and audit behaviours that depend on the taxonomy.

**Out of scope (deferred to a follow-up requirement)**

- Inbound EDI 855/856/997 dispute handling as a distinct intake origin (revisit after Phase 6 if volume warrants).
- Multi-tenant / multi-sales-org taxonomy variants (Z-codes per sales org are modelled but cross-tenant divergence is out).
- Migration of historical (pre-cutover) closed cases beyond derived super-group backfill.

---

## 3. Glossary

| Term | Definition |
|---|---|
| **Case** (`OrderCase`) | A single business unit of work opened by ASOE2 against one customer-PO context. |
| **Child case** | A scoped sub-work-unit under a Case. Today's `ExceptionRecord` table is renamed in API/UI but the table name is retained for migration safety. |
| **Origin** | The intake mechanism. Exactly two values: `CUSTOMER` (customer-initiated intake — email today, web form / portal later), `API` (SAP-pushed block). |
| **Source channel** | Orthogonal metadata describing the upstream channel: `EMAIL`, `EDI_850`, `B2B_PORTAL`, `CSR_KEYED`, `PHONE`, `FAX`. |
| **Intent Super-Group** | Case-level classification. The "Case intent" referred to in the PO chat of 24-May. |
| **Intent** | Leaf classification on a child case. The agent dispatch / routing key. |
| **Steward** | The named Master Data role with write authority over the taxonomy tables. |

---

## 4. Decision log (resolutions of the 27-May open questions)

| # | Question | Decision |
|---|---|---|
| Q1 | Multi-block on API path | **1:N children from day one.** API parents may carry multiple children; the "one block per order" SAP runtime behaviour is an observed invariant, not a schema constraint. |
| Q2 | Strict super-group inheritance | **Strict on both paths in v1.** Triggers reject a child whose `supergroup_code` differs from its parent. A nullable `divergence_reason` column and an operational `inheritance_mode_customer` config flag (default `STRICT`) are reserved so the Steward can relax CUSTOMER inheritance later if production data warrants (§8.1), without a schema migration or PO re-approval. |
| Q3 | Source-channel scope | **Binary origin `{CUSTOMER, API}` + orthogonal `source_channel`.** EDI 855/997 inbound disputes deferred (§2 out of scope). |
| Q4 | API super-group granularity | **8 coarse buckets** (§6.2). Leaf intent carries SAP-code granularity. |
| Q5 | Taxonomy seed source | **Data-mining sprint (Phase 0) precedes contract changes.** 90-day SAP block extract from `VBAK`/`VBAP`/`VBUK` × `TVLST`/`TVFST`/`TVAGT`; 30-day email-classification audit. Seed list in §6.2/§6.3 is the initial commitment; Phase 0 may add but not subtract from this list without PO approval. |
| Q6 | Storage as data vs enum | **DB-managed lookup tables** (§7) with CI-generated constants for compile-time safety. |
| Q7 | "Customer Inbox" UI label | **Keep.** Internal model uses `CUSTOMER`; visible UI chrome remains "Customer Inbox". |
| Q8 | Naming convention | **Prefix discipline.** Super-groups: `SG_*`; intents: `INT_*`. Identical strings across the two domains are banned by lint. |
| Q9 | Steward authority & change SLA | Steward proposes → OM business lead approves → SAP functional lead co-signs for SAP semantics → engineering merges. **Target lifecycle: 3 business days** (data change), **emergency same-day** with retroactive CAB within 48h. |
| Q10 | Pallet semantics | `INT_PALLET_CONFIG` (order-math rounding) and `INT_BROKEN_PALLET` (warehouse handling-fee policy) are **sibling leaves** under `SG_BLOCK_LOGISTICS`. Distinction documented in §6.4. |
| Q11 | Reclassification rights | **Matrix in §8.3.** CSR may self-correct own cases within 24h of creation; team lead may reclassify any open case; model reclassification permitted only at confidence ≥ 0.85 and writes `classifier_type=MODEL`. |
| Q12 | `NEEDS_TRIAGE` forcing functions | **All four adopted:** hard-block at close, 48h auto-age alert, weekly Top-10 review, <3% per-CSR scorecard target. |
| Q13 | SLA inputs | `default_sla_for_intent × rdd_urgency × customer_tier × shelf_life_remaining`. Joint owner: Supply Chain + CS Ops. Super-group provides floor only. Formula in §8.4. |

---

## 5. High-level model

```
Case (OrderCase)
  ├─ origin                CUSTOMER | API
  ├─ source_channel        EMAIL | EDI_850 | B2B_PORTAL | CSR_KEYED | PHONE | FAX
  ├─ supergroup_code       fk → case_supergroup
  ├─ predecessor_case_id   fk → order_case  (null; set on re-block / fork)
  ├─ will_miss_rdd         bool             (replaces the deprecated DELIVERY_DELAY super-group)
  ├─ sla_tier              tier 1–4         (computed at create, §8.4)
  └─ children: 0..N child cases
        ├─ supergroup_code      (trigger-enforced = parent on both origins in v1)
        ├─ divergence_reason    text null   (reserved; always null in v1 — see §8.1)
        ├─ intent_code          fk → case_intent  (LEAF = routing key)
        ├─ sap_block_code       text null
        ├─ sap_block_field      LIFSK|LIFSP|FAKSK|FAKSP|ABGRU|CMGST|Z_CUSTOM | null
        └─ scope                HEADER | ITEM
```

Routing key: **leaf `intent_code`**, never `supergroup_code`. Super-group is reporting/rollup only.

---

## 6. Taxonomy — committed seed lists

### 6.1 Origin

Exactly two values: `CUSTOMER`, `API`. No third origin in this requirement. The visible UI label for the `CUSTOMER` path is **"Customer Inbox"** (unchanged).

### 6.2 API Super-Groups (8, committed)

| Code | Description |
|---|---|
| `SG_BLOCK_PRICING` | Price condition missing/expired, manual price override pending, contract pricing mismatch, mass pricing error. |
| `SG_BLOCK_CREDIT` | Credit limit exceeded, watchlist hold, deduction dispute hold, payment-terms mismatch. |
| `SG_BLOCK_AVAILABILITY` | ATP shortage, allocation exceeded, MOQ violation, OVER_MAX, back-order required, substitution required. |
| `SG_BLOCK_MASTER_DATA` | Ship-to invalid, partner role missing, customer hierarchy issue, material status (discontinued / pre-launch / sample-only). |
| `SG_BLOCK_LOGISTICS` | Pallet rounding (`INT_PALLET_CONFIG`), broken-pallet handling fee (`INT_BROKEN_PALLET`), truck constraint, hazmat segregation, route/calendar issue, delivery-window appointment. |
| `SG_BLOCK_COMPLIANCE` | Tax / VAT-ID missing, customs / export licence hold, hazmat regulatory, country-of-origin documentation. |
| `SG_BLOCK_ORDER_INTEGRITY` | Duplicate PO, EDI mismatch (850 vs SAP), incomplete sales doc, contractual correction needed. |
| `SG_BLOCK_UNMAPPED` | Reserved. Auto-assigned when SAP returns a block code not in `case_intent.sap_block_code`. Triggers P2 ops alert; mapping SLA = 1 business day. |

### 6.3 CUSTOMER Super-Groups (12, committed)

| Code | Description |
|---|---|
| `SG_NEW_ORDER` | New customer purchase order received by email. |
| `SG_ORDER_CHANGE` | Modification to an existing order (qty, date, SKU, ship-to). |
| `SG_ORDER_STATUS_INQUIRY` | "Where is my order" — status / tracking / ETA questions. |
| `SG_SHIPMENT_DISCREPANCY` | Short-ship, over-ship, damaged-in-transit, missing shipment / POD. |
| `SG_RETURN_RGA` | Return authorisation, RGA / RMA, recall reconciliation. |
| `SG_LOGISTICS_CHANGE` | Ship-to address change, routing change, delivery-window change, carrier change request. |
| `SG_BILLING_DISPUTE` | Invoice dispute, payment-terms dispute, freight charge dispute, deduction reconciliation. |
| `SG_DOCUMENTATION` | COA, MSDS, tax / exempt certificate, customs paperwork, sample request. |
| `SG_COMPLAINT_SERVICE` | Service-level complaint (response time, wrong information given, missed callback). |
| `SG_COMPLAINT_PRODUCT` | Product-quality complaint, recall trigger, food-safety report. |
| `SG_EDI_ESCALATION` | Customer reports EDI failure (850/855/810) by email — escalation path when the API channel itself is broken. |
| `SG_NEEDS_TRIAGE` | Cannot classify on intake. Subject to forcing functions in §8.2. |

### 6.4 Leaf Intents — initial seed (existing 13 + additions)

The existing flat `Intent` enum becomes the **initial seed** of `case_intent`, prefixed and remapped to super-groups. New leaves are added by Phase 0 data mining.

| Initial leaf | Super-group | Notes |
|---|---|---|
| `INT_PRICE_MISMATCH` | `SG_BLOCK_PRICING` | renamed from leaf `PRICE_MISMATCH` |
| `INT_MASS_PRICING_ERROR` | `SG_BLOCK_PRICING` | |
| `INT_PRICE_HOLD_RELEASE` | `SG_BLOCK_PRICING` | |
| `INT_CONTRACTUAL_CORRECTION` | `SG_BLOCK_ORDER_INTEGRITY` | |
| `INT_CREDIT_BLOCK` | `SG_BLOCK_CREDIT` | |
| `INT_DUPLICATE_PO` | `SG_BLOCK_ORDER_INTEGRITY` | |
| `INT_EDI_MISMATCH` | `SG_BLOCK_ORDER_INTEGRITY` | |
| `INT_BACK_ORDER` | `SG_BLOCK_AVAILABILITY` | |
| `INT_OVER_MAX` | `SG_BLOCK_AVAILABILITY` | |
| `INT_MIN_ORDER_QTY` | `SG_BLOCK_AVAILABILITY` | |
| `INT_PALLET_CONFIG` | `SG_BLOCK_LOGISTICS` | order-math rounding |
| `INT_BROKEN_PALLET` | `SG_BLOCK_LOGISTICS` | warehouse handling-fee policy |
| `INT_DELIVERY_DELAY` | `SG_BLOCK_LOGISTICS` | demoted from super-group; symptom only |
| `INT_MANUAL_ORDER_INTAKE` | `SG_NEW_ORDER` | |
| `INT_UNMAPPED_PENDING_TAXONOMY` | `SG_BLOCK_UNMAPPED` | |
| `INT_UNKNOWN` | `SG_NEEDS_TRIAGE` | reserved; closure-blocking (§8.2) |

The final leaf list per super-group is produced by Phase 0; PO approves the additions in a 30-minute review.

---

## 7. Storage — taxonomy as data

### 7.1 Tables

```sql
case_supergroup (
  code              text  primary key,        -- SG_*, immutable
  origin            text  check (origin in ('CUSTOMER','API','BOTH')),
  description       text  not null,
  owner_role        text  not null,
  is_active         boolean not null default true,
  effective_from    date  not null,
  deprecated_at     date  null,
  replaced_by_code  text  null references case_supergroup(code),
  sort_order        int   not null default 0,
  version           int   not null default 1
)

case_intent (
  code              text  primary key,        -- INT_*, immutable
  supergroup_code   text  not null references case_supergroup(code),
  description       text  not null,
  sap_block_code    text  null,
  sap_block_field   text  null check (sap_block_field in
                            ('LIFSK','LIFSP','FAKSK','FAKSP','ABGRU','CMGST','Z_CUSTOM')),
  sap_sales_org     text  null,               -- null = global; else per-org Z-code
  is_active         boolean not null default true,
  effective_from    date  not null,
  deprecated_at     date  null,
  replaced_by_code  text  null references case_intent(code)
)

supergroup_intent_allowed (
  supergroup_code   text  references case_supergroup(code),
  intent_code       text  references case_intent(code),
  effective_from    date  not null,
  deprecated_at     date  null,
  primary key (supergroup_code, intent_code)
)

intent_label (
  code              text  not null,           -- joins to case_intent.code OR case_supergroup.code
  domain            text  check (domain in ('SUPERGROUP','INTENT')),
  locale            text  not null,           -- e.g. 'en', 'en-US', 'de-DE'
  display_name      text  not null,
  description       text  not null,
  primary key (code, domain, locale)
)
```

### 7.2 Compile-time safety

A CI step generates Python and TypeScript constants from the active rows of `case_supergroup` and `case_intent`. Contract tests fail the build if code references an inactive or unknown code.

### 7.3 No CHECK constraints on enum values in DDL

Inheritance and "leaf ∈ allowed set" rules are enforced by triggers reading the lookup tables, not by hard-coded CHECK lists. This is the explicit reversal of the 27-May draft.

---

## 8. Behaviour

### 8.1 Inheritance

**v1 rule (both origins, strict):**

| Origin | Rule |
|---|---|
| `API` | Trigger sets `child.supergroup_code := parent.supergroup_code` on insert. Updates that diverge are rejected. |
| `CUSTOMER` | Trigger sets `child.supergroup_code := parent.supergroup_code` on insert. Updates that diverge are rejected. |

**Reserved operational lever (out of scope for v1, no PO approval needed to enable later):**
A runtime config `inheritance_mode_customer ∈ { STRICT, RELAXED }` defaults to `STRICT`. The schema includes a nullable `divergence_reason` column on the child case. If multi-intent customer email volume justifies it, the Steward proposes switching the flag to `RELAXED`; under `RELAXED`, a child may carry a different `supergroup_code` from its parent *only if* `divergence_reason` is populated. The change follows the §9.1 governance workflow (Steward → OM lead approval) and requires no code deploy or schema migration. Switching back to `STRICT` is reversible; existing divergent children are left as-is and surface in a Steward review.

Leaf validity: trigger asserts `(child.supergroup_code, child.intent_code) ∈ supergroup_intent_allowed` for the effective date.

### 8.2 `SG_NEEDS_TRIAGE` forcing functions

1. **Hard-block at close.** A case with `supergroup_code = 'SG_NEEDS_TRIAGE'` cannot transition to `RESOLVED`.
2. **48h auto-age alert.** Any case in `SG_NEEDS_TRIAGE` for >48h surfaces on the team-lead dashboard.
3. **Weekly Top-10.** Steward publishes the 10 most common free-text triage notes; reviewed by PO + CS Ops lead.
4. **Per-CSR scorecard.** Percentage of own cases closed (after reclassification) that *originated* as `SG_NEEDS_TRIAGE` is reported; target <3%.

### 8.3 Reclassification rights

| Actor | Scope | Window |
|---|---|---|
| CSR (case owner) | Own case | ≤ 24h after case creation |
| Team lead | Any open case in their queue | Any time before `RESOLVED` |
| Model classifier | Any case at model confidence ≥ 0.85 | At any classifier event; writes `classifier_type=MODEL` |
| Steward | Any case (for taxonomy corrections only) | Any time; writes `classifier_type=RULE` with reason |

Every reclassification writes one row to `case_classification_history` (§8.6). The current `supergroup_code` / `intent_code` on the case row is the most recent history row.

### 8.4 SLA computation

At case creation:

```
sla_due_at  =  now + default_sla_for_intent(intent_code)
              × rdd_urgency_factor(case.requested_delivery_date, now)
              × customer_tier_factor(case.customer_id)
              × shelf_life_factor(case.line_items)

sla_tier    =  bucketise(sla_due_at - now)   -- 1 (<2h), 2 (<8h), 3 (<24h), 4 (>24h)
```

`default_sla_for_intent` is a column on `case_intent`. Super-group provides a **floor SLA** only: `sla_due_at ≤ now + supergroup_floor_sla`. Joint owner of the factors: Supply Chain lead + CS Ops lead.

### 8.5 Routing

The routing table `route(intent_code) → (queue_id, default_sla_tier)` is owned per functional area. Super-group is **never** a routing input.

### 8.6 Classification audit

```sql
case_classification_history (
  id                   bigserial primary key,
  case_id              uuid not null,
  child_case_id        uuid null,             -- null for parent-level reclass
  supergroup_code      text not null,
  intent_code          text null,             -- null for parent-level
  classified_at        timestamptz not null,
  classified_by        text not null,         -- user id, or 'system:<component>'
  classifier_type      text check in ('HUMAN','MODEL','RULE'),
  model_version        text null,
  reason_text          text null,
  source_event_id      uuid null,
  taxonomy_version_id  text not null          -- snapshot id of the active mapping
)
```

Append-only. No UPDATE / DELETE permitted (DB role enforced). Retention: 7 years.

### 8.7 Re-block linkage

When a previously-resolved API case re-trips (the released order hits the next latent block), the new case sets `predecessor_case_id` to the prior case. Reporting joins on this column expose the "true incident" view (one underlying order, multiple sequential cases).

### 8.8 Unmapped SAP block code

When SAP delivers a block code not present in `case_intent.sap_block_code`:

1. Case is created with `supergroup_code='SG_BLOCK_UNMAPPED'`, `intent_code='INT_UNMAPPED_PENDING_TAXONOMY'`.
2. P2 ops alert raised to the Steward queue.
3. Steward decision SLA: 1 business day.
4. On mapping, retroactive `case_classification_history` row updates the case.
5. Nightly reconciliation job diffs `TVLST`/`TVFST`/`TVAGT` against `case_intent.sap_block_code` and pre-opens steward tickets.

---

## 9. Governance

### 9.1 Change-control workflow

1. **Request:** business or ops opens a taxonomy ticket (new code, rename, deprecation).
2. **Review:** Steward checks for overlap and naming consistency.
3. **Approval:** OM business lead approves; SAP functional lead co-signs if SAP semantics change.
4. **Apply:** Steward edits the versioned YAML in the config repo and opens a PR.
5. **Merge:** engineering reviews migration safety, merges.
6. **Deploy:** DB migration applies the change; CI regenerates constants.
7. **Backfill (if needed):** cases in `SG_BLOCK_UNMAPPED` matching the new code are reclassified with a history row.

**Target lifecycle: 3 business days.** Emergency path (in-prod blocker): same-day insert by Steward with retroactive CAB within 48h.

### 9.2 Naming convention

- Super-group codes: `SG_<TOPIC>`, SCREAMING_SNAKE_CASE.
- Intent codes: `INT_<TOPIC>`, SCREAMING_SNAKE_CASE.
- Codes are **immutable once published**. Renames are modelled as `deprecated_at + replaced_by_code`.
- A lint rule blocks identical suffixes across `SG_*` and `INT_*` (e.g., `SG_FOO` and `INT_FOO` together are not allowed).

### 9.3 Localization

UI fetches display labels from `intent_label(code, domain, locale)`. Locale fallback order: requested locale → `en` → code itself. Codes are never localized.

---

## 10. Backwards compatibility & migration

1. `OrderCase.case_type` (`EMAIL_ENTRY` / `BLOCK`) is **deprecated**. It remains as a generated column for one release, then dropped.
2. `OrderCase.email_classification` is **deprecated** and superseded by `supergroup_code`. One-release overlap.
3. `exception_record` table keeps its name. A view `child_case` is added for API/UI/BI consumers.
4. The flat `Intent` enum in `contracts/models.py` is **removed**; consumers read generated constants from §7.2.
5. `source = 'manual_order' | 'automated_order'` is **renamed** to `origin = 'CUSTOMER' | 'API'`. Old column kept as derived for one release.
6. UI label "Customer Inbox" is **retained**. Internal model uses `CUSTOMER`. No CSR retraining required.
7. Backfill of existing cases:
   - `source='manual_order'` → `origin='CUSTOMER'`, `supergroup_code` derived from old `email_classification` (`NEW_ORDER`→`SG_NEW_ORDER`, etc.; `OTHER`→`SG_NEEDS_TRIAGE`).
   - `source='automated_order'` → `origin='API'`, `supergroup_code` derived from dominant `sap_block_code` of children via the `case_intent.sap_block_code` mapping; unmapped codes → `SG_BLOCK_UNMAPPED`.

---

## 11. Acceptance criteria

A change satisfies this requirement when **all** of the following are demonstrable:

1. A case can be created via the CUSTOMER path with `supergroup_code` set from intake classification, and via the API path with `supergroup_code` derived from the SAP block code through `case_intent.sap_block_code`.
2. An API parent rejects a child whose `supergroup_code` differs from its own.
3. A CUSTOMER parent rejects a child whose `supergroup_code` differs from its own under the v1 default `inheritance_mode_customer = STRICT`. The `divergence_reason` column exists and is always NULL in v1 (relaxation path covered in §8.1; not in scope for v1 acceptance).
4. Any `(supergroup_code, intent_code)` pair not present in `supergroup_intent_allowed` for the effective date is rejected at insert.
5. A case with `supergroup_code='SG_NEEDS_TRIAGE'` cannot be transitioned to `RESOLVED`.
6. SLA tier on a new case reflects the §8.4 formula; super-group floor SLA is never exceeded.
7. Routing dispatches solely on `intent_code`; manipulating `supergroup_code` without changing `intent_code` does not change the queue.
8. An unknown SAP block code creates a case in `SG_BLOCK_UNMAPPED` and emits a P2 ops alert.
9. Every classification or reclassification event produces exactly one `case_classification_history` row stamped with the taxonomy version in effect.
10. Adding a new SAP block-code mapping (data change) is achievable end-to-end in ≤ 3 business days without a code deploy.
11. CI fails when application code references a deprecated or unknown `SG_*` / `INT_*` constant.
12. Backfill produces zero `SG_BLOCK_UNMAPPED` cases for historical block codes already in production volume.

---

## 12. Phased rollout

| Phase | Scope | Exit gate |
|---|---|---|
| **0 — Data-mining sprint (2 wk)** | 90-day SAP block extract + 30-day email classification audit. Produce final `case_intent` seed for PO approval. | PO signs off seed additions. |
| **1 — Lookup tables (1 wk)** | Ship the four taxonomy tables (§7.1), seed from Phase 0 output, generate constants, ship the CI guard. No case-model changes yet. | All acceptance criteria #4, #11 demonstrable on the lookup tables. |
| **2 — Case model (2 wk)** | Add `origin`, `source_channel`, `supergroup_code`, `predecessor_case_id`, `will_miss_rdd`, `sla_tier` to `OrderCase`. Add `supergroup_code`, `divergence_reason`, `intent_code`, `sap_block_*`, `scope` to `exception_record`. Backfill per §10. | Criteria #1, #2, #3, #6, #8 met. |
| **3 — Routing + history (1 wk)** | Routing on leaf only. `case_classification_history` triggers. NEEDS_TRIAGE forcing functions. | Criteria #5, #7, #9 met. |
| **4 — UI label resolution (1 wk)** | UI consumes `intent_label`; "Customer Inbox" retained; locale fallback wired. | UI smoke test in `en` and one non-`en` locale. |
| **5 — Agent + recipe wiring (1 wk)** | `agents/backfill.py` and `recipes/registry.py` consume generated constants. Dispatch unchanged (still leaf). | Existing recipe regression suite green. |
| **6 — Governance go-live (ongoing)** | Steward workflow, 3-day change SLA, nightly SAP reconciliation, NEEDS_TRIAGE dashboards. | Criterion #10 demonstrable; first real steward-driven mapping change shipped in ≤ 3 days. |

---

## 13. Risks & mitigations (residual)

| # | Risk | Mitigation |
|---|---|---|
| R1 | Phase 0 data mining reveals SAP block codes that don't fit the 8 super-groups | Steward + SAP lead may propose **one additional** super-group with PO approval before §11 sign-off is invalidated. More than one addition reopens this requirement. |
| R2 | Multi-intent customer emails force CSRs to misclassify or split aggressively, distorting volume metrics | A weekly Steward report tracks the *would-be-divergence rate* (cases where the CSR opened a sibling case within 1 hour of the first, against the same customer/PO). If the rate exceeds 15% sustained, the Steward proposes switching `inheritance_mode_customer` from `STRICT` to `RELAXED` (§8.1). Operational toggle, no code change, no PO re-sign-off required. |
| R3 | Model classifier confidence threshold (0.85) is too aggressive | Threshold is configurable per-environment. CS Ops can raise it without a code deploy. |
| R4 | Steward 3-day SLA is missed under volume | If three consecutive misses, PO escalates to staffing — not by lowering the bar. |
| R5 | Re-block linkage creates unbounded chains | Reporting view caps chain depth at 5; chains deeper than 5 surface as an ops anomaly for steward review. |

---

## 14. Provenance

- PO direction: chat 12-May → 24-May 2026 (multi-case → intent super-group).
- Codebase baseline: `contracts/models.py`, `db/migrations/V009__order_case.sql`, `api/store.py`, `recipes/registry.py`, `agents/backfill.py`; UI types `asoe-ui/src/types/cases.ts`, `src/types/exceptions.ts`.
- Stress-test: AI-simulated SME review (SAP OTC, CS Ops, Master Data, Supply Chain) — output captured in PR #182 commit history.
- 13 open questions of 27-May resolved in §4 of this document.

---

## 15. Sign-off

| Role | Name | Signature | Date |
|---|---|---|---|
| Product Owner | | | |
| OM Business Lead | | | |
| SAP Functional Lead | | | |
| Master Data Steward | | | |
| CS Operations Lead | | | |
| Supply Chain Lead | | | |
| Engineering Lead | | | |

**Sign-off captures approval of:** §2 scope, §4 decision log, §6 committed seed lists, §7 storage model, §8 behaviours, §11 acceptance criteria, §12 phase gates.

Implementation begins with Phase 0 only after PO + OM Business Lead + SAP Functional Lead signatures are recorded.
