# CaseListPane — Binding Design Decisions (V5.1.1)

**Date:** 2026-05-11
**Source:** `2026-05-11-case-list-pane-design-review.md` pre-read + lens-audit addendum.
**Authority:** PO confirmed the eight design decisions plus the four product-call options below. Live workshop was not convened; the operator acted as chair against the lens recommendations.
**Status:** Binding for implementation. Subsequent code PRs reference this doc by section.

This document is the **minutes**, not the pre-read. Implementation starts from these decisions.

---

## Confirmed decisions (lens-audit-corrected proposals stand)

| Item | Resolution |
|---|---|
| **D1** Filter chip vocabulary | Three-cluster bar (Live / Waiting / Terminal); per-status sub-chips on demand. Status vocabulary sourced from `useHealth.allowed_case_statuses` (backend deliverable). Status labels consolidated into a single `STATUS_LABEL` exported from `src/lib/cases.ts`; the four duplicate definitions are deleted. The two hardcoded `c.status === "OPEN_AWAITING_HUMAN"` comparisons (`exceptions/page.tsx:184`, `inbox/page.tsx:185`) replaced with an `isAwaitingHuman(status)` helper. |
| **D2** Intent filter | Backend adds `child_intents: string[]` to `/api/v1/cases` and `/api/v1/cases/{id}` **response dicts** (NOT the `OrderCase` model, which is `extra="forbid"`); mirrors the `CaseRecordsResponse.aggregated_policy_hits` precedent. UI: multi-select chip group, any-match semantics, sourced from `useHealth.allowed_intents` per Guardrail #1. URL state `?intents=A,B`. |
| **D3** Search operators | Reuse `searchParser`. Operator vocabulary: `po:`, `so:`, `customer:`, `since:`, `intent:`, `status:`. Free-text falls through to fuzzy match over `case_id` / `customer_po_number` / `sales_order_id` / `customer_id`. URL state `?q=`. |
| **D5** Keyboard nav + WCAG roles | Author `useKeyboardListNav` hook from scratch (~60-80 LoC) using `ExceptionListPane.tsx` as the reference shape — the legacy `/exceptions/page.tsx` arrow-key handler was removed in V5.1 and cannot be lifted. Two consumers: `ExceptionListPane` (legacy queue) and new `CaseListPane`. `role="button"` on queue rows → `role="option"` inside `role="listbox"` parent. **Breaking selector update**: `tests/browser/inbox-navigation-chrome.spec.ts:43` must change from `[role="button"]` to `[role="option"]` in the same PR. |
| **D6** Sort order | SLA-urgency default; toggle `sort=sla | sort=recent` (`recent` = `updated_at desc`). URL state `?sort=`. |
| **D7** Pagination | Deferred. Ship the "showing N of M" indicator and the `cases_returned_per_request` SLI (new metric in `api/metrics.py`); re-open cursor pagination only when a tenant approaches 150 open cases. |
| **D8** Master-detail vs full-page | Keep V5.1's thin right-pane shape. CaseListPane is a list-pane reshape only. Full-detail-in-pane stays V5.2 / out-of-scope. Back-nav focus restoration is in scope. |

## Product-call decisions (operator-resolved, 2026-05-11)

### "My queue" default — D4

**Decision:** Opt-in via explicit "Save as default."

The saved view exists out-of-the-box (no shipping change to `useSavedViews`), but does **not** auto-apply on first login. A new CSR sees the full case list scoped to their `assigned_accounts` and clicks the "Save as default" affordance on the saved-view tile if they want to pin it.

**Impact:** Zero-state friction is slightly higher than the SME's "auto-apply" preference, but the audit-traceability concern from Compliance is decisively cleared — no operator can be surprised that their view is filtered. Aligns with the existing `useSavedViews` storage shape (no migration of the apply-on-load semantics needed).

### `child_intents` cache strategy — D2

**Decision:** Precompute + cache on `case_open` / `case_update` events.

Backend maintains an in-process `Dict[case_id, FrozenSet[str]]` keyed by case_id. Cache is populated on the corresponding `case_*` event emission (already shipped in PR #136); invalidated on the same. Read-side `/api/v1/cases` walks the cache instead of `exception_store.list_by_case`.

**Impact:** ~50 additional LoC over the naive loop, but O(1) per case on the read path. Sets us up for the §28.7 SLI ask (case-level disagreement rate) without re-iterating children. Cache invalidation surface is narrow because the only writers of child intent are `_persist_exception` (which always emits one of `case_open` / `case_update` through `case_resolver.materialise_for_event`).

### PR shape — implementation split

**Decision:** Single PR with all 7 pre-build deliverables.

The seven deliverables (backend × 3, frontend × 4) land as one cohesive change. Easier review against this minutes doc, single rollback point, no cross-PR coordination on the breaking Playwright selector update.

**Impact:** ~1500 LoC diff. Acceptable per the operator; CODEOWNERS already gates the right reviewer set on each path.

### jest-axe a11y test coverage

**Decision:** Lands in the same PR as the role swap.

`jest-axe` scaffolding + role-swap a11y tests on `CaseListPane` + the legacy `ExceptionListPane` ship in V5.1.1. Audit elevated this from V5.1.2 nice-to-have to P1; the operator confirmed P1 sequencing.

**Impact:** +1-2 days to the build. Closes the "zero automated a11y coverage" gap the audit identified.

---

## Implementation order (single PR)

1. **Backend** — `/api/v1/health` extension: `allowed_case_statuses` field. Update `tests/test_health_endpoint.py` lock. Regenerate OpenAPI; regenerate UI types.
2. **Backend** — `child_intents` cache module (`api/case_intents_cache.py`); wire invalidation into `publish_case_open` / `publish_case_update` (the helpers already exist per PR #136). Add `child_intents` field to `/api/v1/cases` and `/api/v1/cases/{id}` responses. Test the cache + the response field.
3. **Backend** — extend `GET /api/v1/cases` query params (`intents=`, `status=` multi-value, `since=`, `q=`). Test filter composition.
4. **Backend** — `cases_returned_per_request` SLI in `api/metrics.py`. Test.
5. **Frontend** — `useKeyboardListNav` hook (new). Test the keyboard transitions.
6. **Frontend** — `useSavedViews` v2 shape (additive discriminator `surface: "exceptions" | "cases"`). Storage migration helper one-shot on first load.
7. **Frontend** — consolidate `STATUS_LABEL` + `isAwaitingHuman` helper into `src/lib/cases.ts`. Replace the four duplicates + two hardcoded comparisons.
8. **Frontend** — `CaseListPane` component built from V5.1's existing `/exceptions/page.tsx` queue rendering. Filter chips (cluster + intent + saved views), search box (URL-synced), sort toggle, role swap. Mount on `/exceptions` replacing the inline pane.
9. **Frontend** — update `tests/browser/inbox-navigation-chrome.spec.ts:43` selector for the role swap. Add jest-axe scaffolding + a11y tests on `CaseListPane` and (preserved) `ExceptionListPane`.
10. **Docs** — `tasks.md` §28.5.x marks Item 3 shipped.

---

## Out of scope (V5.1.2 or later)

- Full-detail-in-pane (D8)
- Match-reason announcement on search hits (D3)
- Saved-view rename/delete keyboard parity (D4)
- Server-side cursor pagination (D7) — re-open at any tenant ≥ 150 open cases
- "By customer" / tertiary sort (D6)

---

*Minutes recorded 2026-05-11. Subsequent code PRs reference this doc by section. Pre-read at `2026-05-11-case-list-pane-design-review.md`; the lens audit addendum at the bottom of the pre-read is the source for D1/D2/D5 corrections.*
