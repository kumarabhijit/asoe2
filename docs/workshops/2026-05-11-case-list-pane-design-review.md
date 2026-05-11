# Virtual Workshop — CaseListPane Design Review

**Date:** 2026-05-11
**Type:** Asynchronous virtual workshop — each expert lens contributes a position per design decision; the chair (Frontend Platform Lead + PO) reconciles into a green-lit design before implementation begins.
**Trigger:** Phase 28.5.x Item 3 (CaseListPane). V5.1 closed the `/inbox` + `/exceptions` data-source swap to `casesApi.list`; the rich master-detail features that previously lived on `ExceptionListPane` (intent/lifecycle filter chips, search-with-operators, saved views, arrow-key keyboard nav) were dropped because their vocabularies don't translate cleanly to case-level fields. **Reintroducing those features against the new case shape requires a design pass before implementation.**
**Scope:** Eight design decisions for the V5.1 case-projected master-detail queue. Each lens stakes a position; the workshop reconciles into a binding design.

This document is a **pre-read**, not the workshop minutes. The minutes (with binding decisions) land after the live session.

---

## Participants (virtual lenses)

| Persona | Authority |
|---|---|
| **Frontend Platform Lead** | Owns the V5.1 reshape; load-bearing on the implementation. |
| **Domain SME (CSR workflow)** | Whether the proposed filter / search / saved-view UX matches how real operators triage cases. |
| **Compliance Veto Holder** | Whether the surface preserves audit traceability (no synthesised data, no filter that hides audit-bearing rows). |
| **AI/Agentic Engineering Architect** | Whether the design respects Guardrails #1 (no hardcoded enums) and #6 (no client-side composition of enrichment payloads). |
| **Product Owner (PO)** | Roadmap sequencing; whether the V5.1.1 increment is the right shape vs deferring features to V5.2. |
| **Tools Admin / SRE** | Whether the proposed `casesApi` extensions (filters, pagination, search) load-balance acceptably. |
| **Accessibility Lens** | Whether the keyboard-nav + screen-reader story matches WCAG AA and operator-with-disability workflows. |
| **Data Engineering** | Whether the search operators can be satisfied by current store indices or need new ones. |

---

## Agenda — eight design decisions

### D1 — Filter chip vocabulary: case status vs lifecycle state

**What's at stake:** ExceptionListPane filtered on `lifecycle_state` (PENDING_REVIEW / EXECUTING / RESOLVED / …) and `intent`. Cases have a different vocabulary: `CaseStatus` (`OPEN_AGENT_PROCESSING | OPEN_AWAITING_HUMAN | OPEN_AWAITING_BUYER | OPEN_AWAITING_ERP | RESOLVED | FAILED | BLOCKED`) and no direct intent. Mapping the old filters 1:1 doesn't work.

| Lens | Position | Concerns |
|---|---|---|
| **FE Platform** | Recommend: filter on `CaseStatus` with a "live → waiting → terminal" grouping. The seven values fit naturally into three operator-meaningful clusters that match the existing `lifecycle_state` chip muscle memory. | Worried about chip count: 7 values is too many flat chips. The grouped pattern matters. |
| **Domain SME** | Strong support for grouped chips (Live / Waiting / Terminal). CSRs think in "what needs me now" / "I'm blocked by external party" / "done." A flat list of `OPEN_AGENT_PROCESSING` etc. reads as backend jargon. | Operator-facing labels should be plain English, not the enum. |
| **Compliance** | Approve. As long as terminal cases (RESOLVED / FAILED / BLOCKED) are visible by an explicit chip toggle (not hidden by default), audit traceability holds. | Strong veto on "show-only-open" being the default with no way to surface closed cases — operators must be able to inspect every closed case in scope. |
| **AI/Architect** | Approve. `CaseStatus` literal values come from `contracts/models.py::CaseStatus` — sourced via `useHealth.allowed_case_statuses` (extension needed) to preserve Guardrail #1 (no hardcoded enums in `.tsx`). | Health endpoint extension is a backend ask before the FE work lands. |
| **PO** | Approve grouped chips. | None. |
| **A11y** | Group chips must be `role="group"` with `aria-label` per cluster. Each chip remains `aria-pressed` toggleable; screen reader announces "Live: 12, Waiting: 4, Terminal: 87." | Total counts per cluster must be announced. |

**Anticipated resolution:** Three-cluster chip bar (Live / Waiting / Terminal), with each cluster expandable into per-status sub-chips on demand. Backend deliverable: extend `/api/v1/health` with `allowed_case_statuses` (already in roadmap per FE Platform comment in `useHealth`). Label vocabulary: "Live" / "Waiting" / "Terminal" at cluster level; the seven sub-status labels reuse the existing `STATUS_LABEL` map in `src/app/cases/page.tsx`.

---

### D2 — Intent filter on a case-level surface

**What's at stake:** ExceptionListPane filtered by `intent`. Cases are intent-agnostic — a single case can carry children of multiple intents (a multi-PO email with one CONTRACTUAL_CORRECTION and one DUPLICATE_PO). Should CaseListPane offer intent filtering?

| Lens | Position | Concerns |
|---|---|---|
| **FE Platform** | Recommend: intent filter operates as "show cases with *at least one* child carrying this intent." Backend computes via `exception_store.list_by_case` — same join already used for the partner scope check. | Worried about the response payload: today `casesApi.list` returns OrderCase only. Need a `child_intents` array on the response (compute server-side). |
| **Domain SME** | Want it. The "I'm a CONTRACTUAL_CORRECTION specialist" CSR view is a real workflow. Without intent filter, every CSR sees every case regardless of their team's focus. | Filter should be inclusive (any-match), not strict (all-match). |
| **Compliance** | Approve as long as the filter is **additive only**. A case must never be hidden from a CSR with read scope by intent filtering — they're CHOOSING to narrow the view, not being denied access. | Wants the URL param state (e.g. `?intent=DUPLICATE_PO`) so an operator can share a filtered URL during a triage handoff without losing the audit trail. |
| **AI/Architect** | Approve with caution. Intent values come from `useHealth.allowed_intents` per Guardrail #1. The filter must not encode any intent literal in page code. | None blocking. |
| **PO** | Approve. | None. |
| **SRE** | Backend join cost: `list_by_case` is O(records) per case. For 200 cases × 5 records avg, that's 1k records read per filter pass. Acceptable; can add a `case_id → set<intent>` reverse index later if scale demands. | Want a metric on filter response latency once the build ships. |
| **Data Engineering** | Approve. Suggest the backend precomputes `child_intents` once on case_update / case_open events (already emitted per PR #135) and caches on the OrderCase model. Then filter is in-memory and O(1) per case. | Cache invalidation: child added / removed → recompute on the next case_update. Small footprint (≤ 8 strings/case). |

**Anticipated resolution:** Intent filter as multi-select chips with any-match semantics. Backend extension: `OrderCase` gains a derived (not persisted) `child_intents: list[str]` field on the wire; cached in-process. `casesApi.list` accepts a `intents=` query param (comma-separated, multi-value). URL syncs to `?intents=DUPLICATE_PO,CONTRACTUAL_CORRECTION`.

---

### D3 — Search box: operators + free-text

**What's at stake:** ExceptionListPane carried a `searchParser` with operators like `account:walmart since:7d 1042` (account name, time range, free-term fuzzy match). Cases need their own operator vocabulary.

| Lens | Position | Concerns |
|---|---|---|
| **FE Platform** | Recommend a case-specific operator set: `po:<value>`, `so:<value>`, `customer:<value>`, `since:<7d|24h|today>`, `intent:<value>`, free-text falls through to fuzzy match across `case_id`/`customer_po_number`/`sales_order_id`/`customer_id`. | Reuse the searchParser primitive — the parser doesn't care about the operator vocabulary. The matchers do. |
| **Domain SME** | Strong support. Today CSRs type partial PO numbers and expect them to match. Want one additional operator: `status:waiting_buyer` so an operator can pre-load their "follow-up Friday" view via URL. | Spelling-tolerance on operator names (e.g. `customer` vs `cust`) is nice-to-have. |
| **Compliance** | Approve. Search narrows the view; it never hides audit-bearing data. Same URL-state ask as D2 — sharing a search URL preserves traceability. | None blocking. |
| **AI/Architect** | Approve. Operators are UI sugar over `casesApi.list` query params; the backend remains the single source of truth. | Don't add an operator that the backend can't filter on. |
| **PO** | Approve. | Wants the search box visible on small screens (responsive). |
| **SRE** | Free-text fuzzy match runs client-side over the already-loaded page. Acceptable up to ~500 cases per page; beyond that the FE needs pagination + server-side search. Defer pagination to D7. | None blocking for V5.1.1. |
| **A11y** | Search box must be `<input type="search" aria-label="Search cases">`. Operator chips on the result row need announcement: "matched on PO" / "matched on customer." | Today's ExceptionListPane doesn't announce match reasons; want it added on this iteration. |

**Anticipated resolution:** Reuse `searchParser` with a case-specific operator map. URL sync via `?q=`. Match-reason announcement (A11y ask) is a stretch goal — can land in V5.1.2.

---

### D4 — Saved views

**What's at stake:** Saved views on ExceptionListPane stored `{ filterStates, filterIntents, filterDate, searchQuery }` in localStorage. The shape changes for cases.

| Lens | Position | Concerns |
|---|---|---|
| **FE Platform** | Migrate `useSavedViews` to a generic shape with a `surface: "exceptions" \| "cases"` discriminator. Old saved views stay readable; new ones write under the cases shape. | Storage key bump to `asoe-ui:saved-views:v2`. Migration is one-shot on first load. |
| **Domain SME** | Want one default saved view shipped: "My queue" = `cluster:waiting + customer:<my-assigned-accounts>`. Reduces zero-state friction for a new CSR. | The "My queue" view needs to derive `assigned_accounts` from the session, not be hard-coded. |
| **Compliance** | Approve. Saved views are operator-side preferences; they never alter the underlying audit surface. | None. |
| **AI/Architect** | Approve. Migration path preserves Guardrail #1 — the saved-view shape doesn't embed enum literals. | None. |
| **PO** | Approve the default "My queue." Sequencing: ship the new shape first; the migration helper lands one release later. | None. |
| **A11y** | Saved-view selector must be a labelled combobox, not a button-with-text. Today's `SavedViewsMenu` is a Radix DropdownMenu — verify keyboard nav covers the "rename" / "delete" affordances on each entry. | Today these are mouse-only; need keyboard verification. |

**Anticipated resolution:** Migrate `useSavedViews` to v2 shape. Ship the "My queue" default. Defer keyboard parity on rename / delete to V5.1.2.

---

### D5 — Keyboard navigation: arrow keys + Enter

**What's at stake:** ExceptionListPane had ArrowUp / ArrowDown / j / k / Home / End. Enter on a focused row opened the detail. CaseListPane needs the same.

| Lens | Position | Concerns |
|---|---|---|
| **FE Platform** | Lift the keyboard handler from `src/app/exceptions/page.tsx` into a reusable `useKeyboardListNav` hook. Both surfaces share it. | One implementation, two consumers. |
| **A11y** | Strong support. Today's ExceptionListPane handler is exemplary — preserves it. Add `role="listbox"` on the queue container and `role="option"` on each row so screen readers announce the selection move. | Today's row is `role="button"`; needs change to `role="option"` inside a `role="listbox"` parent. |
| **Compliance** | Defer (no compliance angle). | None. |
| **PO** | Approve. | None. |

**Anticipated resolution:** Extract `useKeyboardListNav` hook + update both surfaces to use `role="listbox"` / `role="option"`. A11y signoff is binding.

---

### D6 — Sort order: SLA urgency vs updated_at desc

**What's at stake:** V5.1 settled `/cases` on SLA urgency (most-urgent first). ExceptionListPane sorted by `updated_at` desc. CaseListPane needs to pick one default.

| Lens | Position | Concerns |
|---|---|---|
| **FE Platform** | Default: SLA urgency. Operator's primary question is "what's about to breach?" not "what changed recently?" — the WS-driven invalidation already shows changes in place. | Want a sort-toggle for power users (advanced operators). |
| **Domain SME** | SLA urgency is correct. The current `/cases` UX already proves this — CSRs scan top-to-bottom and the urgent row is at top. | Want a "Recently changed" toggle that flips to updated_at desc for the "did anything happen since coffee" check. |
| **Compliance** | Defer (no compliance angle). | Wants sort state in the URL so a shared link preserves the operator's view. |
| **PO** | Approve. | None. |

**Anticipated resolution:** SLA urgency default + toggle (sort=sla | sort=recent). URL-synced.

---

### D7 — Pagination: client-side vs server-side

**What's at stake:** ExceptionListPane paginated via cursor (server-side). V5.1 `casesApi.list` returns all-cases-in-one-call up to limit=200/500. At what scale does that break?

| Lens | Position | Concerns |
|---|---|---|
| **FE Platform** | Defer cursor pagination. Today's tenants are well below 200 open cases; the UI's SLA-sort renders correctly on the in-memory list. When a tenant exceeds 200, the empty-list affordance still fires (limit cap = ceiling). | Need a "showing N of M" indicator on the queue so operators know when they've hit the cap. |
| **SRE** | Approve deferral. Track `cases_returned_per_request` SLI; if p99 hits 200 for any tenant, escalate to server-side cursor. | Want the SLI added to `/api/v1/metrics` once V5.1.1 ships. |
| **PO** | Approve. Build for current scale; revisit at the first tenant exceeding 150 open cases. | None. |
| **Compliance** | Approve. If the cap is hit, the empty-affordance row must surface ("showing 200 of N — refine filter to see the rest") so operators don't silently miss cases. | Strong veto on a silent truncation. |

**Anticipated resolution:** Defer cursor pagination. Ship the "showing N of M" indicator and the SLI ask. Re-open when any tenant approaches 150 open cases.

---

### D8 — Master-detail vs full-page

**What's at stake:** V5.1 currently uses a thin right-pane summary on `/inbox` and `/exceptions` with "Open case" → `/cases/{id}` for full detail. ExceptionListPane previously hosted a full detail panel in-pane. Should CaseListPane bring back the in-pane full detail?

| Lens | Position | Concerns |
|---|---|---|
| **FE Platform** | Recommend keep the V5.1 split (thin right-pane + jump to `/cases/{id}`). The full-detail panel coupling was a major source of complexity in the old ExceptionListPane. | Want the thin right-pane to render `attachedRecords` (now shipped in §28.5.x) as a list, so the operator can see "what's in the case" before jumping. |
| **Domain SME** | Mild preference for the full-page detail (less context-switch). But not load-bearing — if it adds 2 weeks of build, defer. | Wants to confirm jumping to `/cases/{id}` doesn't lose filter / search context (back button must work). |
| **PO** | Defer the full-detail-in-pane to V5.2. V5.1.1 ships the rich filter / search / saved-views on the existing thin-right-pane shape. | Scope discipline. |
| **A11y** | Approve. The back-nav must preserve focus on the row the operator left from. | Standard A11y ask; not a blocker. |

**Anticipated resolution:** Keep V5.1 thin-right-pane shape. CaseListPane is a list-pane reshape, NOT a detail-panel reshape. Back-nav focus restoration is in scope.

---

## Open questions for the live workshop

1. **D1**: Confirm the three-cluster grouping (Live / Waiting / Terminal) and assign the per-cluster status mapping.
2. **D2**: Confirm backend cache strategy for `child_intents` (recompute on `case_update` event vs on-demand).
3. **D3**: Approve the operator vocabulary (`po:`, `so:`, `customer:`, `since:`, `intent:`, `status:`) — any missing?
4. **D4**: Confirm "My queue" default is opt-in (operator clicks "Save as default" once) vs forced on first login.
5. **D5**: Confirm the role change (`role="button"` → `role="option"` inside `role="listbox"`) — A11y signs off.
6. **D6**: Confirm sort toggle (sla | recent) is sufficient — no third option like "by customer" needed.
7. **D7**: Confirm SLI for `cases_returned_per_request` is added to the Phase 28.6 Grafana dashboard.
8. **D8**: PO confirms full-detail-in-pane stays V5.2 / out-of-scope for V5.1.1.

---

## Pre-build deliverables (gate Item 3 implementation)

Listed in implementation order; each is a small backend or design artefact that unblocks the FE build.

1. **Backend — `useHealth.allowed_case_statuses`** (D1). Extend `/api/v1/health` payload + UI's `useHealth` hook to surface the `CaseStatus` literal. Today these are hardcoded in `src/app/cases/page.tsx::STATUS_LABEL` — V5.1.1 must remove that.
2. **Backend — `OrderCase.child_intents` derived field** (D2). Add a derived list on the wire, populated by walking `exception_store.list_by_case` per case. Optional cache (premature; ship the naive path first, escalate if SLI shows latency).
3. **Backend — `casesApi.list` query params** (D2, D3). `intents=`, `status=` (multi-value), `q=`, `since=`. URL → API mapping defined.
4. **Frontend — `useKeyboardListNav` hook extraction** (D5). Lift from `src/app/exceptions/page.tsx`. Two consumers: `ExceptionListPane` (legacy queue) and the new `CaseListPane`.
5. **Frontend — `useSavedViews` v2 shape** (D4). Add the `surface` discriminator; ship the migration helper.
6. **Frontend — A11y role swap** (D5). `role="listbox"` / `role="option"` on both surfaces in the same PR.

---

## Out of scope (V5.1.2 or later)

- Full-detail-in-pane (D8) — deferred to V5.2.
- Match-reason announcement on search hits (D3) — V5.1.2.
- Keyboard parity on saved-view rename / delete (D4) — V5.1.2.
- Server-side cursor pagination (D7) — only when a tenant approaches 150 open cases.
- "By customer" / "By account" tertiary sort (D6) — only if domain SME flags need.

---

## Addendum — Lens audit findings (2026-05-11)

Three lens-audit passes ran on the pre-read above and surfaced material corrections to D1, D2, and D5. Each correction is binding for implementation; the workshop reconciles them in the live session and confirms.

### D1 — chip vocabulary (corrected)

The pre-read claimed "today `STATUS_LABEL` is hardcoded in `src/app/cases/page.tsx`." **Wrong scope.** The audit found:

* `STATUS_LABEL` is duplicated across **four** UI files: `src/app/cases/page.tsx` (lines 65–73), `src/app/cases/CaseDetailPanel.tsx` (lines 48–56), `src/app/exceptions/page.tsx` (lines 78–86), `src/app/inbox/page.tsx` (lines 77–85).
* Two of those files (`exceptions/page.tsx:184`, `inbox/page.tsx:185`) embed hardcoded `c.status === "OPEN_AWAITING_HUMAN"` comparisons in metric-count logic — a Guardrail #1 violation independent of the workshop's chip-vocabulary question.
* The "FE Platform comment in useHealth" the pre-read cites for the `allowed_case_statuses` roadmap **does not exist**. The wire field doesn't exist and the hook has no roadmap notation.

**Binding correction:** D1 implementation MUST (a) collapse the four `STATUS_LABEL` definitions into a single shared map (consume from `useHealth.allowed_case_statuses` once the backend ships it, with a typed fallback constant in `src/lib/cases.ts` for the transition window), (b) replace the three hardcoded `"OPEN_AWAITING_HUMAN"` comparisons with a `isAwaitingHuman(status)` helper, (c) backend deliverable for `allowed_case_statuses` is real engineering work, not a comment-only change.

### D2 — child_intents wire shape (corrected)

The pre-read proposed "add a derived `OrderCase.child_intents` field." **Wrong layer.** The audit found:

* `OrderCase` (`contracts/models.py:154–209`) uses `ConfigDict(extra="forbid")`. Adding a Pydantic field requires a contract-model change, which has compliance + version-tracking implications.
* The cleaner pattern already exists in the codebase: `aggregated_policy_hits` on `CaseRecordsResponse` (the §28.5.x loader) — server adds the field to the **response dict** after `model_dump()`, never on the model itself.
* Cost is acceptable: `_scope_to_user` already pays the O(N × R) loop for partner / assigned-account scoping; `child_intents` adds O(M) per case at most.
* No existing test pins the OrderCase JSON round-trip, so adding the field post-serialization breaks nothing.

**Binding correction:** D2 implementation MUST add `child_intents` to the **response** of `list_cases` / `get_case`, NOT to the `OrderCase` Pydantic model. Mirror the `CaseRecordsResponse.aggregated_policy_hits` pattern. The UI's `casesApi.list` / `casesApi.get` return-type stays a Record-extending alias rather than `OrderCase` strict.

### D5 — keyboard nav + WCAG roles (corrected)

The pre-read said "lift `useKeyboardListNav` from `src/app/exceptions/page.tsx`." **Wrong source.** The audit found:

* The V5.1 reshape **removed** the arrow-key / Home / End handler. The current `/exceptions` page only has Enter/Space on each row (line 491–493). The pre-read assumed the legacy handler was still present.
* The handler must be authored **new** (~60–80 LoC). The state surface is clean — single `selectedId` + sorted array — so the extraction shape is still right, but it's a write, not a refactor.
* `ExceptionListPane.tsx` (the legacy queue, still rendered at `/exceptions/[id]` parent) already implements `role="listbox"` + `role="option"` correctly — use it as the reference for the new hook.
* One Playwright test locks the old role: `tests/browser/inbox-navigation-chrome.spec.ts:43` queries `[role="button"][aria-label^="Select case"]`. The role swap is a **breaking change** for that selector and must be updated in the same PR.
* Codebase has **zero `jest-axe` coverage**. The role swap will not be caught by automated a11y CI; the V5.1.2 follow-up to add a11y tests on the queue page becomes critical (not nice-to-have).

**Binding correction:** D5 implementation MUST (a) author `useKeyboardListNav` from scratch using `ExceptionListPane` as the reference shape, (b) update `inbox-navigation-chrome.spec.ts:43` and any other selector-by-role test in the same PR, (c) elevate the a11y-test follow-up from V5.1.2 nice-to-have to a P1 (must land within 2 PRs of D5).

---

*Pre-read authored 2026-05-11 by Frontend Platform. Lens-audit addendum landed 2026-05-11. Live workshop convenes the lenses above; binding decisions land in the `2026-05-11-case-list-pane-decisions.md` minutes after.*
