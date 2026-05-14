# ADR-041: Case-Type Axis + `/cases` Workspace Consolidation

**Status:** Accepted (2026-05-13 — all sub-phases merged across asoe2 + asoe-ui)
**Date:** 2026-05-13
**Deciders:** Principal AI/Agentic Engineering Architect; Frontend Platform; Domain Modeller; UX Architect; DevOps; Product Owner.
**Applies to:**
* asoe2: `contracts/models.py::OrderCase`, `api/store.py::ExceptionRecord`, `api/case_resolver.py`, `tests/test_routes_cases.py`, `tests/test_case_type_invariants.py`, `.github/workflows/deploy-azure.yml`, `docs/test-strategy/README.md`, `CLAUDE.md`.
* asoe-ui: `src/types/cases.ts` + `src/types/exceptions.ts`, `src/app/cases/page.tsx` + `src/app/cases/[id]/page.tsx` + `src/app/cases/CaseDetailPanel.tsx`, `src/lib/api.ts` + `src/lib/mock-data/*`, `src/config/nav-tabs.ts`, `next.config.mjs`, `tests/architectural/cases_workspace_render_guard.test.ts` + `case_pivot_mock_wiring.test.ts`, `tests/browser/cases-workspace-case-switch.spec.ts`, `docs/test-strategy/README.md`, `CLAUDE.md`. Deletions: `src/app/exceptions/page.tsx` + `error.tsx` + `loading.tsx` + `ExceptionListPane.tsx` + `CaseListPane.tsx` + `SavedViewsMenu.tsx` + `searchParser.ts`.

**Related:**
* ADR-038 (case-centric order intake — defines `OrderCase` parent + lazy materialisation).
* ADR-038 §H.6 + S15a (per-record HITL ribbon retired `/exceptions/[id]` in favour of inline mount on `/cases/[id]?record=<id>`).
* ADR-040 (case-level four-eyes cosign).

---

## 1. Context

ADR-038's case-centric pivot retired `/exceptions/[id]` (S15a) and made
`/cases/[id]?record=<id>` the per-record HITL surface. Five gaps
surfaced over the following two weeks of operator feedback + panel
reviews:

1. **The asoe2 detail-path visibility was asymmetric.** `GET
   /api/v1/cases/{id}` applied `_scope_to_user` (assigned-account
   filter) while `GET /api/v1/exceptions/{id}` was tenant-only. An
   analyst could fetch a child record but get 404 on the parent case
   — and `/cases/[id]?record=<id>` deep-links would render only the
   header.

2. **The asoe-ui mock data drifted from the backend's S15a
   invariant** (`api/case_resolver.py::should_materialise() -> True`
   — every record has a parent case). No `MOCK_EXCEPTIONS` entry had
   `parent_case_id` set; `casesApi.getRecords()` always returned `[]`
   in mock mode; preview-mode operators saw the case header with no
   action ribbon. The systemic gap: mock-drift from backend
   invariants is invisible to CI because test coverage biases toward
   live-backend specs.

3. **The PO requested a two-axis case taxonomy** —
   `case_type ∈ {EMAIL_ENTRY, BLOCK}` with sub-classification:
   EMAIL_ENTRY classified per intake (NEW_ORDER / ORDER_CHANGE /
   INQUIRY / COMPLAINT / OTHER); BLOCK typed per SAP block reason
   code on each child record. The PO suggested folding the axis
   into the existing `source` field (manual_order / automated_order);
   the domain modeller's panel review pushed back — they answer
   different questions.

4. **The UI carried two queue surfaces.** `/exceptions` (with
   `CaseListPane`) and `/cases` (a simpler list) both projected the
   same OrderCase data. Architect panel framing: "paying rent on a
   synonym." The post-S15a `/cases/[id]?record=<id>` workspace was
   the canonical action surface but the queue surface was unsettled.

5. **Test strategy systematically missed multi-step state-machine
   bugs.** A case-switch race (clicking case A then case B in fast
   succession left the URL at `?case=B&record=<record-belonging-to-A>`,
   rendering the right pane without an action ribbon) shipped because
   no test in the suite drove the queue → detail flow as an operator
   does it. Every browser spec deeplinked to a single record. The
   bug surfaced "many cases don't show detailed view" — the precise
   symptom the PO reported.

Plus a sixth, infra-side gap that wasn't UI:

6. **Azure deployment was manual.** `scripts/deploy-azure.sh`
   existed but was invoked from the operator's laptop. No CI gate,
   no health-check, no automatic rollback.

ADR-041 ratifies the answers to all six.

## 2. Decision

### 2.1 Two-axis domain typing (P1)

Add to `contracts/models.py::OrderCase`:

* `case_type: Literal["EMAIL_ENTRY", "BLOCK"]` — orthogonal to
  `source`. `source` answers "how did the order originate?";
  `case_type` answers "why did ASOE materialise this case?".
  Defaults from `source` via a `mode="before"` Pydantic
  `model_validator` (`infer_case_type`) so all existing call sites
  keep working unmodified. Examples where the axes disagree:
    * A `manual_order` arriving by **phone** is NOT EMAIL_ENTRY.
    * An `automated_order` (EDI 850) that gets SAP-blocked IS BLOCK.
* `email_classification: Optional[Literal["NEW_ORDER",
  "ORDER_CHANGE", "INQUIRY", "COMPLAINT", "OTHER"]]` — required when
  `case_type == "EMAIL_ENTRY"` (defaults to "OTHER"); forced None
  when BLOCK. Enforced by `_check_case_type_invariants` (mode="after"
  validator).

Add to `api/store.py::ExceptionRecord`:

* `sap_block_code: Optional[str]` — raw SAP block reason on
  BLOCK-parented records (1:N — one SAP order can carry multiple
  simultaneous codes). Distinct from `intent` (the classified
  business-intent vocabulary recipes dispatch on).

Mirror on the UI side (`src/types/cases.ts`, `src/types/exceptions.ts`).
Mock fixtures (`src/lib/mock-data/cases.ts`) set `case_type` from
event_type so every mock case carries the new axis.

Deferred soft invariants (called out in the validator for follow-up):
"EMAIL_ENTRY ⇒ source_email_id required" + "BLOCK ⇒ sales_order_id
required" — turning these on today would regress test fixtures and
sandbox flows that don't always populate those correlation keys.
Tracked against `compliance/audit_bearing_registry.yaml::grandfather_clauses`.

### 2.2 Detail-path visibility symmetry (PR #152 / #153)

`GET /api/v1/cases/{id}` and `GET /api/v1/cases/{id}/records` are now
**tenant-scoped only** on the detail path (no `_scope_to_user`
filter). The list endpoint (`GET /api/v1/cases`) retains the
account-scope filter as a UX queue filter — operators still see
their own queue by default, but deep-links to a case detail resolve.

Paired with `api/case_resolver.py::should_materialise() -> True` —
every persisted record gets a parent case unconditionally. The S15a
invariant is now both enforced (backend) and locked
(`tests/test_routes_cases.py::TestDetailVisibilityInvariant` — every
exception visible to a user implies the parent case is too, across
analyst / manager / viewer / assigned-analyst / partner).

### 2.3 `/cases` is the single canonical workspace (P2 / P3 / P4)

`/exceptions` route family is retired. `next.config.mjs` permanently
redirects `/exceptions` and `/exceptions/:path*` to `/cases`. NavBar
drops the "Exception Queue" tab. The route files (`page.tsx`,
`error.tsx`, `loading.tsx`, `ExceptionListPane.tsx`,
`CaseListPane.tsx`, `SavedViewsMenu.tsx`, `searchParser.ts`) are
deleted (~2600-line net deletion).

`/cases` is the **two-pane workspace** — case queue on the left
(SLA-sorted, filter chips, keyboard nav, pin-selection guard), case
detail on the right (inline `CaseDetailPanel` mounting
`ExceptionDetailPanel` for the selected record). URL-driven via
`?case=<caseId>&record=<recordId>` so back/forward + reload preserve
cursor position. `/cases/[id]` survives as the focused single-case
deep-link target (no queue chrome).

Two source-locked race-fix invariants (P3c):

1. The fetch `useEffect` on `[selectedCaseId]` clears `orderCase` /
   `records` / `policyHits` BEFORE the new `casesApi.get` call.
2. The JSX renders `CaseDetailPanel` only when
   `orderCase.case_id === selectedCaseId`.

One source-locked pin-selection invariant (P3d): the `cases`
useMemo lists `selectedCaseId` in deps AND produces an
`isPinned: true` row when the active filter excludes the selection
(so a WS-driven refetch that flips the selected case's status
doesn't yank the operator's row out from under them).

WS silent live refresh restored on `/cases/page.tsx`:
`useCases().refetch` is wired to `useWebSocket({ onEvent,
onReconnect, onPollFallback })`. Keyboard nav (`useKeyboardListNav`):
↑/↓/j/k/Home/End on the queue listbox.

### 2.4 Mock-data structural split (P5)

`src/lib/api.ts` shrunk from 3894 → 2146 lines (45% reduction). Bulk
mock fixtures extracted into `src/lib/mock-data/`:

| Module | Purpose |
|---|---|
| `mock-data/order-analyses.ts` | `MOCK_ORDER_ANALYSES` — keyed by ExceptionSummary.id |
| `mock-data/exceptions.ts` | `MOCK_EXCEPTIONS` + the S15a `parent_case_id` forEach wiring in module scope |
| `mock-data/cases.ts` | `caseFromMockException` + `MOCK_CASES` derived 1:1 from MOCK_EXCEPTIONS |
| `mock-data/line-items.ts` | `MOCK_LINE_ITEMS` |

Zero consumers needed updating — every `MOCK_*` identifier is
referenced from within api.ts only.

### 2.5 Azure deploy automation (P6)

`asoe2/.github/workflows/deploy-azure.yml` wraps
`scripts/deploy-azure.sh`:

* Triggers on push to `main`, gated on the `pytest tests/` workflow
  being green for the same SHA (via `lewagon/wait-on-check-action`).
* `workflow_dispatch` for manual re-runs.
* OIDC federated identity (`azure/login@v2`) — no long-lived
  `AZURE_CREDENTIALS` JSON; rotation automatic.
* Health-check polls `/api/v1/health` for 60s after deploy.
* Rollback on failure via `az containerapp revision deactivate` —
  ACA's blue-green default IS the rollback path.

`asoe-ui` stays on Vercel (DevOps panel verdict: two-cloud is
intentional; Vercel preview-per-PR is genuinely better than ASA for
this surface).

### 2.6 Test-strategy gap closure

Six systemic gaps identified during the post-mortem of the
case-switch race; closure codified in
`docs/test-strategy/README.md` (both repos) + `CLAUDE.md` gates:

1. **Multi-step "operator journey" coverage** — pattern at
   `tests/browser/cases-workspace-case-switch.spec.ts` (clicks two
   queue rows in sequence, asserts URL `?record=` always belongs to
   URL `?case=`).
2. **Race / stale-state coverage** — source-level architectural
   lock pattern + behavioural test pattern. Reference impls at
   `tests/architectural/cases_workspace_render_guard.test.ts`
   (three invariants).
3. **Mock-data layer drift from backend invariants** — every
   Pydantic `model_validator` requires a matching UI mock
   architectural lock. Reference impl at
   `tests/architectural/case_pivot_mock_wiring.test.ts`.
4. **Agent-driven-mutation tests** — deferred; the pin-selection
   guard (§2.3) closes the visible-symptom half.
5. **Visual regression baseline** — deferred; documented gap.
6. **"Regression test per bug" policy** — codified as a
   CLAUDE.md gate in both repos. Bug-fix PRs must include a test
   that fails on the parent commit; verify procedure documented.

## 3. Why not collapse `case_type` into `source`?

The PO's initial proposal was to rename `manual_order` →
`EMAIL_ENTRY` and `automated_order` → `BLOCK`. The domain modeller's
panel review pushed back:

* `source` is **how the order originated**. Allowed values:
  manual_order (email / phone / fax) vs automated_order (EDI X12 /
  portal / API feed / FTP / VMI).
* `case_type` is **why ASOE materialised this case**. Allowed
  values: EMAIL_ENTRY (customer email arrived) vs BLOCK (SAP order
  blocked).

Examples where the two disagree:

* A `manual_order` arriving by phone is NOT EMAIL_ENTRY (no email).
* An `automated_order` (EDI 850) that gets SAP-blocked IS BLOCK
  (the order is automated but the trigger is the block).
* An EMAIL_ENTRY classified `INQUIRY` may not produce a SAP order
  at all.

Conflating them loses information. Keeping both axes lets future
channels (e.g., a phone-call trigger; a portal-blocked
acknowledgement) extend cleanly. The price is one extra field on
OrderCase + one Pydantic validator — both cheap.

## 4. Why retire `/exceptions` rather than redirect `/cases` → `/exceptions`?

The S15a pivot had already established `/cases/[id]?record=<id>`
as the canonical per-record HITL surface. The remaining choice was
the queue-route name. Architect panel verdict: `/cases` is the
honest framing — operators work cases (the SOX-audit boundary);
exceptions are the per-event records inside. The `/exceptions`
route was a historical artefact of the pre-pivot world.

## 5. Edge cases / unanswered questions

* **EMAIL_ENTRY without `source_email_id`.** The hard invariant
  "EMAIL_ENTRY ⇒ source_email_id required" is deferred. Today the
  validator accepts EMAIL_ENTRY cases without it (back-compat for
  test fixtures + sandbox flows). Production flow must populate it;
  enforcement is a follow-on once the ADR-041 ingestion audit lands.

* **BLOCK without `sales_order_id`.** Same shape as above —
  deferred soft invariant. The block-event ingestion paths today
  don't always carry the SO id at construction; the hard rule waits
  for that audit.

* **Mock-trace dispatcher extraction.** `MOCK_TRACE_*` in
  `src/lib/api.ts` (~600 lines) is the next-biggest extraction
  target after P5; intricate enough that it wants its own focused
  PR. Tracked in `tasks.md` Phase 15.10.

* **Three-pane workspace** (lift the record picker into its own
  column + responsive collapse rules below 1280px / 1024px). UX
  architect panel's full vision; P3a/b/c/d shipped two-pane. The
  three-pane refactor is queued as P3d-remaining.

## 6. Definition of Done

ADR-041 is **Accepted** (matches actual state as of 2026-05-13):

* **Domain typing (P1):** `case_type` + `email_classification` on
  OrderCase, `sap_block_code` on ExceptionRecord. 15 new pytest
  invariants in `tests/test_case_type_invariants.py`. UI types
  mirrored. ✅ merged 2026-05-13 (asoe2 #154 + asoe-ui #156).
* **Detail-path visibility symmetry:** case-GET tenant-only; child
  visibility invariant locked. ✅ merged 2026-05-12 (asoe2 #152) +
  invariant test merged 2026-05-13 (asoe2 #153).
* **Mock-data wiring:** every mock exception has parent_case_id;
  MOCK_CASES derived 1:1; arch lock at `case_pivot_mock_wiring.test.ts`.
  ✅ merged 2026-05-13 (asoe-ui #155).
* **`/cases` two-pane workspace (P3a–P3d slice):** URL-driven layout,
  WS silent refresh, keyboard nav, case-switch race fix (two-layer
  source lock + browser e2e), pin-selection guard. ✅ merged
  2026-05-13 (asoe-ui #157 + #158 + #160).
* **`/exceptions` retirement (P2 + P4):** redirect, NavBar tab
  removal, route files deleted, arch locks refactored. ✅ merged
  2026-05-13 (asoe-ui #156 + #157).
* **Mock-data extraction (P5):** api.ts 3894 → 2146 lines. ✅
  merged 2026-05-13 (asoe-ui #160).
* **Azure deploy automation (P6):** workflow + OIDC + health-check +
  rollback + docs. ✅ merged 2026-05-13 (asoe2 #154).
* **Test-strategy doc + CLAUDE.md gates:** ✅ merged 2026-05-13
  (asoe-ui #159 + asoe2 #155).

Deferred follow-ons (not in DoD):
* P3d-remaining (three-pane refactor + finer responsive collapse).
* P5 follow-on (MOCK_TRACE_* extraction).
* Hard `source_email_id` / `sales_order_id` invariants per §5.
* Visual regression baseline (test-strategy Gap 5).
* Agent-driven-mutation behavioural tests (test-strategy Gap 4).

---

*End of ADR-041.*
