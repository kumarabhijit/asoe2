# Case & Intent Super-Group — Draft Requirements

> **Status:** DRAFT for SME review. Do not implement.
> **Origin:** PO conversation 12-May → 24-May 2026; AI-simulated SME stress-test by SAP OTC architect, CS Operations manager, Master Data steward, and Supply Chain SME personas.
> **Purpose:** Produce the final, reviewable requirement for unifying ASOE2's two case-intake paths under a single hierarchical intent taxonomy.

---

## 1. Background

### 1.1 PO direction (as stated)

- Customer email → creates a **Case**, classified as one of: `New Order`, `Order Change`, `Inquiry`, `Complaint`, `Others`.
- SAP-blocked order (today labelled "API" / formerly "Automated") → creates a **Case** whose type derives from the SAP block-reason code (e.g., `Duplicate PO`, `Price Mismatch`, `Broken Pallet`).
- **Case intent = Intent Super-Group.** A parent case carries one super-group; child cases inherit it.
- Leaf `Intent` (today's flat 13-value enum) remains the **agent dispatch key** under each super-group.

### 1.2 Resolved design assumptions (per PO, 27-May)

| # | Assumption | Status after SME review |
|---|---|---|
| A1 | One block code per SAP order (1 parent : 1 child on API path) | **Contested.** See §3.1 |
| A2 | Child case never has a super-group different from parent | **Contested for EMAIL.** See §3.2 |
| A3 | Two enums + `origin` discriminator (EMAIL / API); matches current Manual/Automated grouping | **Largely retained.** See §3.3 |
| A4 | UI label "Customer Inbox" maps to EMAIL origin | **Retained.** Keep visible label as-is. |
| A5 | Leaf Intent taxonomy stays as the granular layer under each super-group | **Retained.** |
| A6 | Full Api super-group list will be sourced from the company's SAP block-reason codebook | **Retained but expanded — mine 90 days of SAP data, do not whiteboard.** |

---

## 2. Original proposal (the starting point)

```
Case
  ├─ origin:               EMAIL | API
  ├─ intent_super_group:   one of {EmailSG | ApiSG}    -- discriminated by origin
  └─ children: 0..N ChildCase
                ├─ intent_super_group  (STRICTLY inherited from parent)
                └─ intent              (leaf, from flat Intent enum)

EmailIntentSuperGroup = { NEW_ORDER, ORDER_CHANGE, INQUIRY, COMPLAINT, OTHER }
ApiIntentSuperGroup   = { DUPLICATE_PO, PRICE_MISMATCH, BROKEN_PALLET,
                          CREDIT_BLOCK, PALLET_CONFIG, DELIVERY_DELAY, … }
Leaf Intent           = (existing 13-value enum)

Routing key = leaf Intent
Strict inheritance enforced via DB CHECK + trigger
```

---

## 3. SME findings & revised positions

### 3.1 "One block per SAP order" — **REJECT as a schema constraint**

**Finding (SAP OTC + Supply Chain consensus):** SAP returns one block because evaluation short-circuits (credit → ATP → pricing → delivery). The order has 3–4 latent blocks; ASOE2 will see them sequentially as each prior block is released. In real CPG OTC books, multi-block orders are 20–35% of blocked volume. Hard-coding 1:1 cardinality bakes a wrong shape into FKs and dispatch.

**Revised position:**
- Model API origin as **1:N from day one**, identical to EMAIL.
- Treat "single block in flight" as a runtime invariant we **observe**, not a schema constraint we **enforce**.
- Add a **re-block linkage** so a re-tripped order links back to its original parent rather than appearing as a new case ("successor child" or `predecessor_case_id`).

### 3.2 Strict super-group inheritance — **RELAX for EMAIL**

**Finding (CS Operations primary, SAP OTC secondary):** ~25–35% of customer emails touch multiple POs or multiple intents ("ship the backorder AND change the delivery date AND why was I charged freight"). Strict inheritance forces CSRs into one of two bad outcomes: corrupt the classification, or split into multiple parents and destroy the inbound-volume metric.

**Revised position:**
- For **API origin**: keep strict inheritance. SAP-driven cases have a single deterministic block context.
- For **EMAIL origin**: allow **mixed-super-group children** under one parent. Parent super-group is the *triage call*; child super-groups can differ. Audit trail records both.
- Add a **case-split** relationship type with explicit lineage ("forked from `<case_id>`") so CSRs can split when needed without double-counting inbound volume.

### 3.3 `origin` shape — **KEEP binary, ADD source_channel**

**Finding (SAP OTC):** EDI / B2B portal / CSR-keyed orders all become SAP-resident docs that flow through the same block machinery. The intake mechanism is binary (email vs API); the *channel* the original order came from is orthogonal metadata that drives resolution playbook (cannot email an EDI customer for clarification — must send 855/997).

**Revised position:**
- `origin ∈ { EMAIL, API }` — binary intake mechanism (unchanged).
- Add `source_channel` as orthogonal attribute: `{ EMAIL, EDI_850, EDI_855_DISPUTE, B2B_PORTAL, CSR_KEYED, PHONE, FAX, … }`.
- Consider a third origin `EDI_INBOUND` *only* if ASN/855 disputes are in scope (they don't come through SAP block flow). Deferred to phase 2 unless PO confirms.

### 3.4 API super-group taxonomy — **RESHAPE, do not whiteboard**

**Finding (SAP OTC + Supply Chain):** Three issues with the proposed list:
1. SAP has **four orthogonal block fields**, not one: delivery block (`VBAK-LIFSK` / `VBAP-LIFSP`, table `TVLS`), billing block (`VBAK-FAKSK` / `VBAP-FAKSP`, `TVFS`), reason for rejection (`VBAP-ABGRU`, `TVAG`), and credit status (`VBUK-CMGST` — a status, not a reason code).
2. The proposed list is at **block-code granularity** masquerading as super-groups. The same two-character code means different things across `TVLS` / `TVFS` / `TVAG`. Every CPG layers Z-codes per sales org.
3. The list is conspicuously thin for a CPG order book. Missing at minimum: ATP shortage, allocation exceeded, MOQ, material status (discontinued / pre-launch), substitution required, ship-to invalid, truck constraint, hazmat / regulatory, customer labelling, delivery-window appointment, incoterm / export docs, customer-specific Z-codes.

**Revised position:**
- Make ApiSuperGroup **coarser** (5–8 values), with leaf Intent carrying SAP-code granularity. Proposed coarse buckets:
  - `BLOCK_PRICING`
  - `BLOCK_CREDIT`
  - `BLOCK_AVAILABILITY` (ATP / allocation / back-order)
  - `BLOCK_MASTER_DATA` (ship-to, partner roles, customer hierarchy, material status)
  - `BLOCK_LOGISTICS` (pallet config, pallet handling policy, truck, hazmat, route)
  - `BLOCK_COMPLIANCE` (tax, customs, hazmat-regulatory, export licence)
  - `BLOCK_ORDER_INTEGRITY` (duplicate PO, EDI mismatch, incomplete sales doc)
  - `BLOCK_UNMAPPED` (reserved — see §3.7)
- Each SAP block code maps to **one** coarse super-group. Multiple codes per super-group is expected and desirable.
- **Do not freeze the list in a workshop.** Mandate a 90-day extract from `VBAK` / `VBAP` / `VBUK` × `TVLST` / `TVFST` / `TVAGT` joined to actual block frequency, before code is written.

### 3.5 EMAIL super-group taxonomy — **EXPAND from 5 → ~15**

**Finding (CS Operations):** Five EMAIL super-groups will collapse under real volume. Within a quarter, `OTHER` will hit 40%+ because the model has nowhere to put: short-ship / over-ship, damaged in transit, missing / lost shipment (POD), return / RGA request, shipping-address change, payment-terms dispute, COA / documentation request, sample request, tax / exempt certificate, EDI failure escalation.

**Revised position — proposed EmailSuperGroup list:**
```
NEW_ORDER                  Order Change                INQUIRY
SHIP_STATUS                SHORT_OR_OVER_SHIP          DAMAGED_IN_TRANSIT
MISSING_SHIPMENT_POD       RETURN_RGA                  ADDRESS_OR_ROUTING_CHANGE
INVOICE_OR_PAYMENT_DISPUTE COA_OR_DOCUMENTATION        SAMPLE_REQUEST
TAX_OR_EXEMPT_CERT         EDI_FAILURE_ESCALATION      COMPLAINT_SERVICE
COMPLAINT_PRODUCT          NEEDS_TRIAGE  (was "OTHER" — see §3.8)
```
Names are placeholders — see §3.6 for naming convention; final list to be set by CS Ops + Data Steward after a 30-day email-classification audit.

### 3.6 Naming / overlap — **PREFIX discipline, separate display label**

**Finding (Master Data + SAP OTC):** `DUPLICATE_PO` appearing as both super-group AND leaf is a governance smell that will produce ambiguous BI within a year.

**Revised position:**
- Machine codes use prefix discipline: `SG_…` for super-groups, `INT_…` for leaves. Or use separate namespaces (`supergroup.code` vs `intent.code`) with a lint check banning identical strings across them.
- Machine code is **immutable once published**.
- Display label is a separate column, locale-aware (`label(code, locale)`).
- Reports and APIs emit `code`; UI resolves label at render time.

### 3.7 Storage shape — **DATA, not enum**

**Finding (Master Data primary, all four secondary):** Encoding the taxonomy as Python enums with DB CHECK constraints is the single biggest long-term risk. Enums cannot be deprecated without a deploy, cannot effective-date, cannot be soft-deleted, and remove the Data Steward from the loop — exactly the failure mode of the last two CPG taxonomy cleanups.

**Revised position:**
- Taxonomy lives in **DB-managed lookup tables** seeded from versioned YAML in a config repo.
- Schema (proposed):
  ```
  case_supergroup(
    code              PK,        -- immutable, SCREAMING_SNAKE_CASE
    origin            EMAIL|API|BOTH,
    description       text,
    owner_role        text,
    is_active         bool,
    effective_from    date,
    deprecated_at     date null,
    replaced_by_code  fk null,
    sort_order        int,
    version           int)

  case_intent(
    code              PK,
    supergroup_code   fk,
    description       text,
    sap_block_code    text null,   -- only API-side intents
    sap_block_field   text null,   -- LIFSK|LIFSP|FAKSK|FAKSP|ABGRU|CMGST|Z_CUSTOM
    sap_sales_org     text null,   -- null = global, else per-org Z-code
    is_active         bool,
    effective_from    date,
    deprecated_at     date null,
    replaced_by_code  fk null)

  supergroup_intent_allowed(supergroup_code, intent_code, effective_from, deprecated_at)
                                                                 -- enforces "leaf ∈ parent SG" with effective dates

  intent_label(code, locale, display_name, description)         -- localization
  ```
- Compile-time safety preserved via a CI step that generates TypeScript/Python constants from the active rows of the lookup tables, plus contract tests that fail if code references a deprecated value.
- **Steward authority:** Master Data Steward proposes, OM business lead approves, SAP functional lead co-signs for SAP semantics; engineering merges. No engineer-only changes.

### 3.8 The "OTHER" trap — **rename to NEEDS_TRIAGE with forcing functions**

**Finding (CS Operations):** Every CRM rollout produces a 30–60% "OTHER" bucket within six months unless governed.

**Revised position:**
- Rename `OTHER` → `NEEDS_TRIAGE` so it reads as a queue, not a category.
- Auto-age: cases in `NEEDS_TRIAGE` > 48h ping the team lead's dashboard.
- Weekly "Top 10 NEEDS_TRIAGE reasons" report owned by a named Data Steward (not a committee).
- **Hard-block at close:** a case cannot reach `RESOLVED` while super-group = `NEEDS_TRIAGE`. Closure requires a real classification.
- CSR scorecard line item: % of own cases closed as `NEEDS_TRIAGE`, target < 3%.

### 3.9 Unmapped SAP block codes — **never reject, route to UNMAPPED**

**Finding (Master Data + SAP OTC):** When SAP introduces a new block code mid-quarter (Z-codes especially), ASOE2 will see cases with no super-group mapping.

**Revised position:**
- Reserved super-group `BLOCK_UNMAPPED` with leaf `INT_UNMAPPED_PENDING_TAXONOMY`.
- Auto-route, raise P2 ops alert, SLA mapping decision to 1 business day.
- Nightly reconciliation job diffs SAP's `TVLST` / `TVFST` / `TVAGT` against ASOE2's lookup; auto-opens steward ticket for any new code.

### 3.10 Routing key — **always the leaf, never the super-group**

**Finding (Supply Chain + SAP OTC):** `BLOCK_CREDIT` super-group can resolve to AR cash-app team (deduction dispute), credit ops team (limit breach), or compliance (watchlist) — three different queues, same super-group. Routing on super-group breaks the moment any super-group has multiple downstream owners.

**Revised position:**
- Routing table: `(intent_code → queue → default_SLA_tier)`, reviewed by each functional owner.
- Super-group is **reporting-only** (rollup / pivots / steward dashboards).
- Document this rule prominently in the spec.

### 3.11 SLA — **at case instance, not super-group**

**Finding (Supply Chain):** Same leaf intent (`PALLET_CONFIG`) on a DSD same-day route is P1; on a 5-day-lead-time DC drop is P4. Super-group provides default; case context provides override.

**Revised position:**
- SLA computed at case creation from `{leaf default} × {RDD urgency} × {customer tier} × {shelf-life remaining}`.
- Stored on the case row.
- Super-group / leaf provide **floor SLA only**, never ceiling.

### 3.12 Audit trail — **append-only classification history**

**Finding (Master Data):** Reclassification will happen (triage → resolver re-buckets). Without history, complaint metrics get retroactively rewritten and trending becomes meaningless.

**Revised position:**
- Append-only `case_classification_history(case_id, supergroup_code, intent_code, classified_at, classified_by, classifier_type [HUMAN|MODEL|RULE], model_version, reason_text, source_event_id, taxonomy_version_id)`.
- Trigger writes a history row on every classification change; never UPDATE in place without history.
- "Intake mix" reports use the *first* classification; "resolution mix" reports use the *current* — both available.
- Retention: 7 years for CPG dispute / audit windows.

### 3.13 `DELIVERY_DELAY` — **demote**

**Finding (Supply Chain):** It's a symptom (the order will ship late), not a cause (ATP / capacity / carrier / customs). Modelling it as a super-group creates a magnet bucket and breaks routing.

**Revised position:**
- Drop `DELIVERY_DELAY` as a super-group.
- Add `will_miss_rdd` as a boolean case attribute, derived from RDD vs current ETA.
- Force human triage to the actual cause super-group.

### 3.14 BACK_ORDER / OVER_MAX / MIN_ORDER_QTY — **reshape**

**Finding (Supply Chain):** These are first-class fulfilment problems with clear owners, not leaf flavours.

**Revised position:**
- Promote `MOQ_VIOLATION` and `OVER_MAX_OR_ALLOCATION` to super-group level (under `BLOCK_AVAILABILITY`).
- Keep `BACK_ORDER` as a **resolution path** (leaf under `BLOCK_AVAILABILITY > ATP_SHORTAGE`), since back-ordering is one of several responses (others: substitute, short-ship, cancel line).

---

## 4. Revised model (one-page reference)

```
Case
  ├─ origin                EMAIL | API
  ├─ source_channel        EMAIL | EDI_850 | B2B_PORTAL | CSR_KEYED | PHONE | FAX | …
  ├─ supergroup_code       fk → case_supergroup  (parent classification)
  ├─ predecessor_case_id   fk null              (for re-blocks / splits)
  ├─ split_lineage         fk null              (for EMAIL split-from)
  ├─ will_miss_rdd         bool                 (replaces DELIVERY_DELAY supergroup)
  ├─ sla_tier              computed at create from intent × context
  └─ children: 0..N ChildCase
         ├─ supergroup_code   (API: strictly inherited; EMAIL: may differ)
         ├─ intent_code       fk → case_intent  (LEAF = routing key)
         ├─ sap_block_code    text null
         ├─ sap_block_field   LIFSK|LIFSP|FAKSK|FAKSP|ABGRU|CMGST|Z_CUSTOM null
         └─ scope             HEADER | ITEM  (which level the SAP block sits at)

Taxonomy is data: case_supergroup, case_intent, supergroup_intent_allowed,
                  intent_label  (all DB-managed, steward-owned, effective-dated)

Audit: case_classification_history  (append-only, taxonomy-version-stamped)

Routing key:  intent_code (leaf), never supergroup_code
SLA:          stored on case row, computed at create, super-group = floor only
```

---

## 5. Open questions for the PO + real CPG SMEs

> These are the calls the AI-simulated review surfaced but cannot make. Recommend resolving these in a 60-minute working session with: PO, OM business lead, SAP functional lead, Master Data Steward, CS Ops lead, Supply Chain lead.

1. **Multi-block on API path** — confirm we move to 1:N child cases on API from day one (not deferred). Cost: one extra row + one loop; benefit: no schema migration in 6 months.
2. **EMAIL mixed-super-group children** — confirm relaxation of strict inheritance on EMAIL path with an explicit split / fork relationship. CS Ops considers this Day-1 critical.
3. **Source-channel scope** — agree the channel list (EMAIL / EDI / portal / phone / fax / CSR-keyed). Confirm whether inbound EDI 855/997 disputes are in scope (would warrant a third `EDI_INBOUND` origin) or deferred.
4. **API super-group buckets** — confirm move from block-code-granularity (10+) to **5–8 coarse buckets** (`BLOCK_PRICING`, `BLOCK_CREDIT`, `BLOCK_AVAILABILITY`, `BLOCK_MASTER_DATA`, `BLOCK_LOGISTICS`, `BLOCK_COMPLIANCE`, `BLOCK_ORDER_INTEGRITY`, `BLOCK_UNMAPPED`).
5. **SAP-data-driven taxonomy seed** — approve a 90-day extract of `VBAK` / `VBAP` / `VBUK` × `TVLST`/`TVFST`/`TVAGT` before the API leaf-intent list is frozen. Approve a parallel 30-day email-classification audit before the EMAIL super-group list is frozen.
6. **Storage as data vs enum** — confirm taxonomy lives in DB lookup tables (steward-writable) with code-generated constants for compile-time safety, **not** as Python enums.
7. **`Customer Inbox` label** — confirm we keep the visible UI label as-is (CS Ops strongly recommends; internal model still says `EMAIL`).
8. **Naming convention** — pick: prefix discipline (`SG_…` / `INT_…`) vs separate namespaces with a lint check. Either is acceptable; pick one and freeze.
9. **Steward authority & change-control SLA** — agree the request → review → approve → deploy lifecycle and a 3–5 business day target for a new block-code mapping (data change, not code change).
10. **Pallet semantics** — confirm `PALLET_CONFIG` (order math, rounding) vs `BROKEN_PALLET` (warehouse handling-fee policy) are sibling leaves under `BLOCK_LOGISTICS`, documented to prevent CSR misclassification.
11. **Reclassification rights** — who can re-bucket: CSR self, lead, model with confidence threshold? Document the matrix.
12. **`NEEDS_TRIAGE` forcing functions** — approve: hard-block at close, 48h auto-age alert, weekly Top-10, < 3% CSR scorecard target.
13. **SLA computation inputs** — confirm the inputs (`RDD urgency`, `customer tier`, `shelf-life remaining`) and the formula. Owner: Supply Chain + CS Ops jointly.

---

## 6. Phased roadmap (proposed, not committed)

| Phase | Scope | Gating decision |
|---|---|---|
| **0 — Data-mining sprint (2 wk)** | 90-day SAP block extract + 30-day email classification audit. Produce the actual taxonomy seed. | Approve §5 Q5. |
| **1 — Contracts + lookup tables (1 wk)** | Add `case_supergroup`, `case_intent`, `supergroup_intent_allowed`, `intent_label`, `case_classification_history`. Seed from phase-0 output. No API/UI changes yet. | Approve §5 Q4, Q6, Q8. |
| **2 — Case model migration (2 wk)** | Add `origin`, `supergroup_code`, `source_channel`, `predecessor_case_id`, `will_miss_rdd`, `sla_tier`. Backfill from `source` + `email_classification` + `sap_block_code`. Deprecate `case_type` + `email_classification` as derived. | Approve §5 Q1, Q3, Q13. |
| **3 — Routing + SLA wiring (1 wk)** | Route on leaf; SLA at instance. Add `case_classification_history` triggers. | Approve §5 Q10, Q11, Q12. |
| **4 — UI rename + label resolution (1 wk)** | Origin labels (`Customer Inbox` stays), super-group display from `intent_label`. | Approve §5 Q7. |
| **5 — Backfill agent + recipes (1 wk)** | Update `agents/backfill.py`, `recipes/registry.py`. Dispatch unchanged (still leaf). | — |
| **6 — Governance go-live (ongoing)** | Steward workflow, nightly SAP reconciliation, NEEDS_TRIAGE dashboards. | Approve §5 Q9. |

---

## 7. Risks if shipped as originally proposed (no changes)

| # | Risk | Likelihood | Impact |
|---|---|---|---|
| R1 | Multi-block SAP orders re-trip and appear as new cases, ops perceives "bot is broken" | High | High |
| R2 | Strict EMAIL inheritance forces CSRs to corrupt classification or double-count inbound | High | High — destroys ROI baseline |
| R3 | `OTHER` super-group hits 40%+ within 4 months | Near-certain | High |
| R4 | Enum-coded taxonomy accumulates stale values; steward locked out | Certain over 12 months | Medium-High |
| R5 | New SAP Z-codes break case creation until next deploy | Quarterly | Medium |
| R6 | BI reporting incoherent across EMAIL and API origins (no cross-walk) | High | Medium |
| R7 | Supergroup-vs-leaf name collision (`DUPLICATE_PO`) creates ambiguous reports | Certain | Low-Medium |
| R8 | `DELIVERY_DELAY` becomes a magnet bucket, breaks root-cause analysis | High | Medium |

---

## 8. Provenance of this document

- **Source 1:** PO ↔ team chat, 12-May → 24-May 2026 (excerpted at top of conversation).
- **Source 2:** Codebase scan of `/asoe2` (`contracts/models.py`, `db/migrations/V009__order_case.sql`, `api/store.py`, `recipes/registry.py`, `agents/backfill.py`) and `/asoe-ui` (`src/types/cases.ts`, `src/types/exceptions.ts`).
- **Source 3:** AI-simulated SME review by four personas (SAP OTC Solution Architect, CS Operations Manager, Master Data / Process Owner, Supply Chain / Fulfilment SME). **These are AI-generated stress-tests, not actual SME sign-offs.** All §3 findings and §5 open questions must be validated with real SMEs before this document becomes the approved requirement.
