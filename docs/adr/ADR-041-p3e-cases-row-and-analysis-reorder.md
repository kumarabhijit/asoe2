# ADR-041 P3e: Cases Queue Row Expansion + Right-Pane Analysis Reorder

**Status:** Proposed (2026-05-28)
**Date:** 2026-05-28
**Phase:** Sub-phase P3e of ADR-041 (`/cases` workspace consolidation).
**Deciders:** Customer Service Lead (CSA panel); Compliance / Internal Audit SME; Recipe / Business SME; Frontend Platform; Design System owner.
**Applies to:**
* asoe2: `contracts/models.py::OrderCase` (no schema change — see §3.1), `api/schemas.py::CaseListItem`, `api/analysis_composer.py` (per-intent one-liner templates), `api/case_resolver.py` (rollup event dispatch), `compliance/audit_bearing_registry.yaml`, `tests/architectural/`.
* asoe-ui: `src/app/cases/page.tsx`, `src/app/cases/CaseDetailPanel.tsx`, `src/app/exceptions/ExceptionDetailPanel.tsx`, `src/components/ui/VerdictDot.tsx` (new), `src/lib/format.ts` (new), `src/hooks/useRowDensity.ts` (new), `src/lib/api.ts::CaseListItem`, `next.config.mjs` (env var), `tests/architectural/`, `tests/browser/`.

**Related:**
* ADR-041 (parent — case-type axis + workspace consolidation).
* ADR-038 (case-centric order intake).
* ADR-039 §4.5 (compliance shadow L1 vs L2 distinction — referenced by audit rail).
* Compliance Verdict 2026-04-22 (Pillar 2 — `build_analysis` is the sole composer; no UI-side enrichment).

---

## 1. Context

ADR-041 P3a–P3d shipped the two-pane workspace. Two weeks of operator
feedback + a 2026-05-28 cross-functional panel surfaced three
productivity gaps:

1. **The left-pane queue row is information-starved.** Today's row
   carries origin badge, SLA badge, case ID, status (`page.tsx:553-626`).
   Operators must click into every case to learn whether it is a
   $40 short-shipment or a $40K credit block. The CSA panel measured
   ~3-4 cases out of 10 re-ranked once richer context lands on the
   row.

2. **The right-pane reading order is cognitively backwards.**
   `AgentReasoningCard` (recommendation + action ribbon,
   `ExceptionDetailPanel.tsx:562`) sits **above**
   `AgentAnalysisSection` (problem / root cause / recommendation
   narrative, `ExceptionDetailPanel.tsx:658`). On HITL cases the
   narrative is already auto-expanded
   (`defaultOpen={isHumanInTheLoopState(detail.lifecycle_state)}`), so
   the operator does see both — but in the wrong order. The natural
   evaluation sequence is diagnose → recommend → act; today's order
   pushes Approve up the page.

3. **Compliance Hits + classification history compete with the
   action surface for screen real estate.** Both are consulted maybe
   1-in-30 decisions; both today occupy a full block in the main
   column on every case.

The 2026-05-28 panel ran five independent reviews (CSA, Compliance,
Recipe SME, Frontend Platform, Design System). Findings are
synthesised in §2.

---

## 2. Decision

Three coupled changes behind a single feature flag
`NEXT_PUBLIC_CASES_ROW_V2`, default OFF until Phase 0 conditions
(§4) are met.

### 2.1 Richer left-pane row (panel-trimmed anatomy)

Replace today's row with a four-line layout:

* **Left edge:** 3px stripe — `--color-brand-info` when Pinned,
  transparent otherwise. (CSA panel: pin primacy regressed when the
  badge competed with line-1 chips.)
* **Line 1 — chips (size="sm"):** origin · SLA · status · `VerdictDot`
  (new R/A/G primitive; see §2.4).
* **Line 2:** `case_id` (mono, truncate) · `customer_name`
  (truncate, `min-w-0`).
* **Line 3:** `top_line_sku_code` (mono) — `top_line_sku_title` —
  `problem_one_liner` (single `truncate`, full text in `title=` +
  Tooltip on focus).
* **Line 4:** `grid-cols-[1fr_auto]` — intent badge (left), currency
  amount (right, `font-mono tabular-nums`).

All seven new fields render through `<EvidenceBlock>` (Guardrail #6
— no `?? "—"` fallbacks). `customer_name` is cosmetic (the only
optional bypass).

**Density toggle:** `useRowDensity` hook persists `"compact" |
"comfortable"` to `localStorage`. Compact collapses to lines 1+3
only. Toggle lives in the queue header next to the keyboard hint
(`page.tsx:482`). Mirrors `useSavedViews.ts:79-92` SSR-safe pattern.

**RBAC:** Backend strips `dollar_impact` from the list-endpoint
response for users without `exceptions:approve` OR `exceptions:override`.
The UI never sees the field for unauthorised callers; line 4
collapses gracefully via `EvidenceBlock` Structurally Omitted.

### 2.2 Right-pane section reorder + sticky action ribbon

* Move `AgentAnalysisSection` **above** `AgentReasoningCard` in the
  right-pane mount order.
* **Keep** today's auto-expand predicate
  `defaultOpen={isHumanInTheLoopState(detail.lifecycle_state)}` — do
  not widen to non-HITL queue cases. (CSA panel: 70% of cases are
  trust-the-recipe; expanding narrative on those is pure scroll cost.)
* **Visual demotion** of Agent Analysis: `bg-surface-secondary`,
  `border-border-subtle`, no shadow. Recommendation card keeps
  `bg-surface-primary` + `shadow-sm` so the action ribbon still
  reads as "do this."
* **Sticky action ribbon:** Lift Approve/Reject/Override/Escalate/
  Re-analyze out of `AgentReasoningCard` into a thin `ActionRibbon`
  component with `position: sticky; top: 0` within the right-pane
  scroll container. The card body (confidence bar + recipe name +
  `explanation`) stays in flow below the sticky ribbon.
* **Skip-to-actions link:** `<a href="#action-ribbon" className="sr-only
  focus:not-sr-only">` at the top of the slim case header
  (`CaseDetailPanel.tsx:183-227`).

**Explicit non-changes (Compliance vetoes upheld):**
* `trace.explanation` prop on `AgentReasoningCard` stays. The
  one-line policy "why" is distinct from `analysis.diagnosis` (the
  existing comment at `ExceptionDetailPanel.tsx:583-590` documents
  this).
* `ClassificationHistoryPanel` stays in the main column. The §8.6
  audit lineage must be on-screen at the moment of Approve click for
  SOX evidence-of-review.

### 2.3 Compliance Hits demotion (conditional)

* **xl breakpoint (≥1280px):** widen grid at `page.tsx:454` to
  `xl:grid-cols-[360px_minmax(0,1fr)_320px]`. Audit rail is the
  third column: `bg-surface-secondary`, `border-l border-border-subtle`.
  Renders `ComplianceHitsRail` (lifted from
  `CaseDetailPanel.tsx:331-358`). Always visible.
* **lg breakpoint (1024-1279px):** in-pane segmented toggle
  "Workspace ↔ Audit" in the right-pane header.
* **<lg:** stacks below the workspace (no behavioural change).

**Persistent count badge** on the slim case header (Compliance
reversal condition):
* Non-dismissible when `policyHits.length > 0`.
* Click scrolls to / opens the rail.
* Auto-opens rail when `audit_verdict_color ∈ {"R", "A"}`.
* In the keyboard tab order with `aria-describedby` naming the hit
  count.

### 2.4 New UI primitives

* **`src/components/ui/VerdictDot.tsx`** — 8px dot + single-letter
  label, bound to existing `--color-{success|warning|error}` via
  `verdictVariant()`. `aria-label="Audit verdict: Red"` etc. WCAG
  1.4.1 satisfied by the letter pairing (no color-alone reliance).
* **`src/lib/format.ts`** — `formatCurrency(amountCents, currency,
  locale?)` wrapping `Intl.NumberFormat` with `style: "currency"`,
  `currencyDisplay: "narrowSymbol"`. Companion
  `formatCurrencyForA11y` strips the symbol and appends the
  currency word (avoids NVDA/JAWS/VoiceOver pronunciation drift).
* **`src/hooks/useRowDensity.ts`** — SSR-safe `localStorage`-backed
  density preference under key `asoe.cases.rowDensity`.

### 2.5 Explicit out-of-scope (panel rejected or charter-limited)

* **Auto-expand Agent Analysis on non-HITL queue cases** — REJECTED
  by CSA + Compliance + Frontend Platform. Today's HITL gate is
  correct. Compliance reversal condition: ship telemetry showing
  >50% of HITL approvals are clicked without the Analysis section
  ever expanded.
* **Strip `explanation` from `AgentReasoningCard`** — Compliance
  VETO upheld. The field carries the policy basis being approved.
* **Move `ClassificationHistoryPanel` out of the main column** —
  Compliance VETO upheld.
* **Customer-tier / Nth-case-this-week signal** — CSA Miss #1.
  Requires backend customer-service work; separate ticket.

---

## 3. Backend dependencies

The flag stays OFF until all four ship.

### 3.1 `CaseSummary` projection on the list endpoint

`api/schemas.py::CaseListItem` extends with seven new fields. NOT
on `contracts/models.py::OrderCase` (Guardrail #3 — the model
mirror is reserved for persisted shape).

| Field | Type | Tier | Producer |
|---|---|---|---|
| `customer_name` | `Optional[str]` | cosmetic | customer-service lookup at list time |
| `top_line_sku_code` | `Optional[str]` | audit-bearing | rollup writer |
| `top_line_sku_title` | `Optional[str]` | audit-bearing | rollup writer |
| `problem_one_liner` | `Optional[str]` | audit-bearing | `build_analysis` per-intent templates |
| `intent` | `Optional[str]` | audit-bearing | child rollup; runtime enum |
| `dollar_impact` | `Optional[DollarImpact]` (`amount_cents: int, currency: str`) | audit-bearing | recipe; null where Recipe SME flagged ambiguous |
| `audit_verdict_color` | `Optional[Literal["R", "A", "G"]]` | audit-bearing | shadow rollup; respects Recipe SME's never-RED / never-GREEN gates |

Backend owns the "top line by absolute dollar impact" pick rule
(Recipe SME §4); UI does not sort line items client-side.

### 3.2 Transactional rollup guarantee

Any child-record mutation flipping `audit_verdict_color`,
`problem_one_liner`, or `dollar_impact` MUST emit a `case_update`
event for the parent case_id from the same database transaction as
the child `lifecycle_state` write. Locked by architectural test
under `tests/architectural/`, mirrored by
`asoe-ui/tests/architectural/case_pivot_mock_wiring.test.ts`.

### 3.3 Per-intent one-liner templates

Recipe team implements the per-intent templates from the Recipe SME
panel response (verbatim in `tickets/CASE-SUMMARY-recipe-templates.md`).
Two known gaps:

* **PRICE_HOLD** missing `sku` + `qty` on `PriceHoldAnalysisData` —
  either extend recipe output or PRICE_HOLD ships variance-only
  template + hidden dollar column.
* **EMAIL_COMPLAINT (pure intake)** has no quantity fields — either
  add `complaint_analysis` recipe output or EMAIL_COMPLAINT intake
  ships with no one-liner.

Resolution route: extend recipe output (Pillar 1, Verdict 2026-04-22)
OR document under `compliance/audit_bearing_registry.yaml::grandfather_clauses`
with a compliance-approved deadline. No UI-side composition.

### 3.4 RBAC `dollar_impact` filter

`exceptions:approve` OR `exceptions:override` required for the
field to appear on the list response. Integration test asserts the
field is absent for read-only tokens.

---

## 4. Phase gates

| Phase | Owner | Contents | Gate |
|---|---|---|---|
| **Phase 0** | asoe2 | §3.1 schema; §3.2 dispatch + lock; §3.3 templates; §3.4 RBAC | Deployed to staging; all locks pass |
| **Phase 1** | asoe-ui | §2.4 primitives (VerdictDot, formatCurrency, useRowDensity); `CaseListItem` widening + mock fixtures | Component sweep + axe pass; SSR-safe; ships independently |
| **Phase 2** | asoe-ui | §2.1 row anatomy; §2.2 reorder + sticky ribbon; §2.3 audit rail + count badge | All behind flag (default OFF); architectural locks + state-machine browser e2e per CLAUDE.md test strategy |
| **Phase 3** | Cross-functional | Flip flag to ON | (a) Phase 0 in prod; (b) Compliance sign-off on rendered-snapshot audit record; (c) Design System dark-mode parity; (d) CSA dry-run reorders ≥3 of 10 sample cases; (e) telemetry live (row-click, time-to-first-action, Analysis scroll depth) |
| **Phase 4** | follow-on | Customer-tier signal; telemetry-driven re-evaluation of expand-on-all-queue | Tracked in separate tickets |

---

## 5. Consequences

### Positive
* Triage moves from "click every case to learn its weight" to
  "scan-and-rank." Panel CSA expects ~30-40% reduction in
  speculative case-opens.
* Reading order on HITL cases aligns with cognitive sequence
  (diagnose → recommend → act).
* Sticky ribbon makes "scroll-to-act" impossible regardless of
  Analysis length.
* Compliance Hits + audit verdict + classification history all
  remain in the SOX evidence-of-review snapshot (panel veto upheld).
* All new audit-bearing fields go through `EvidenceBlock` — no new
  partial-truth surfaces.

### Negative / accepted trade-offs
* Half as many rows above the fold on a 900px viewport (5 vs 12).
  Mitigated by density toggle.
* Three-column at xl reduces workspace width — verified ≥600px
  workspace at 1280 display.
* Backend takes on per-intent one-liner generation as a
  recipe-resident responsibility (correct per Verdict 2026-04-22 /
  CLAUDE.md Guardrail #6, but new recipe surface).

### Risks (full register in implementation plan)
* Rollup-event drift between queue chips and detail pane — mitigated
  by §3.2 lock + flag stays OFF until verified.
* PRICE_HOLD / EMAIL_COMPLAINT intent templates ship hidden until
  recipe gaps closed — partial-truth avoided.
* `localStorage` density preference does not sync across devices —
  acceptable for v1.

---

## 6. References

* Panel synthesis: 2026-05-28 review with CSA, Compliance,
  Recipe SME, Frontend Platform, Design System.
* CLAUDE.md (asoe-ui) Guardrails #1, #2, #3, #5, #6, #7.
* CLAUDE.md (asoe2) Guardrails 1, 2, 4, 6.
* ADR-039 §4.5 (L1 vs L2 compliance shadow distinction).
* Verdict 2026-04-22 (UI types as product commitment; `build_analysis`
  is sole composer).
