# Gap Remediation — Autonomous Rollout Plan

**Document type:** Operational planning doc (not an ADR — see ADR-034 / ADR-038 / ADR-039 for binding decisions)
**Status:** Active
**Date:** 2026-05-11
**Owners:** Autonomous agent, supervised by `@kumarabhijit`
**Tracks:** post-#133 PO supersession; case-centric exception queue (V5.1.2); ADR-034 Phase G email-source surface

Source documents (read before starting any session):
1. Gap report — produced 2026-05-11 (in chat transcript on branch `claude/analyze-asoe-gaps-0LzG5`).
2. Prioritised plan v1.0 — produced 2026-05-11 (same transcript).
3. Reviewer-panel feedback — produced 2026-05-11 (same transcript). Three required amendments are encoded as P0.1, P1.0, P2.0 below.
4. `constraints/specs.py::_CURATED_INTENT_REASON_TAGS` — binding reason-tag vocabulary.
5. `api/routes/health.py::_REASON_TAGS_BY_INTENT` — wire contract the UI must match.

## Decisions ratified 2026-05-11 (open questions closed)

1. **Snapshot sync direction.** Sibling-checkout. The asoe-ui sync script reads `../asoe2/constraints/specs.py` from a sibling working tree on disk. Cross-repo work in the same session is a companion-PR pair (one PR per repo, same branch name).
2. **PR ownership.** All work stacks onto **`claude/analyze-asoe-gaps-0LzG5`** in each repo as separate commits. Each session = one commit (or a small tight commit group). asoe2 PR #144 is the umbrella; asoe-ui gets its own PR on its `claude/analyze-asoe-gaps-0LzG5` branch when the first commit lands there.
3. **S13 ADR amendment is its own session.** Renumbered: **S13 = ADR-034 §6.2 amendment** (decide if `EMAIL_ORDER_ENTRY_REQUEST` is permanent or transitional). **S14 = rename completion**. S14 is blocked until S13 merges.

---

## 1. Status table (the autonomous agent updates this every session)

All work stacks on `claude/analyze-asoe-gaps-0LzG5` in each repo (no per-session branches).

| Session | Phase | Repo(s) | Scope | Status | Commit |
|---|---|---|---|---|---|
| S0 | scaffold | asoe2 | This planning doc + status table | MERGED | `a1fdaf0` (PR #144) |
| S1 | P0.1 — `reason_tag` casing fix | asoe-ui | Action hook + mock health alignment + first 3 scenarios | IN_PROGRESS | `5caf9d1` (asoe-ui PR #148) |
| S2 | P1.0 — failing CSA one-task test (PO amendment) | asoe-ui | One skip-marked failing test capturing `/cases/[id]` deferral | IN_PROGRESS | `6a9d3b2` (asoe-ui PR #148) |
| S3 | P2.0 — mirror asoe2 stub conformance suite (Boris/SRE/Compliance amendment) | asoe-ui | UI-side mock-conformance test suite + `slo_category` tagging | IN_PROGRESS | `c28efe0` (asoe-ui PR #148) |
| S4 | P1.1 — cursor pagination on case-projected queue | both | **Two-stage land:** S4a — `useCases({limit})` + truncation disclosure (`e213bf1`). S4b — ADR-038 §D7 amendment: backend gains cursor + has_more (asoe2 `ef09070`); UI cursor loop + un-skip deferred test (asoe-ui `c1a990a`). | IN_PROGRESS | `ef09070` (asoe2) + `c1a990a` (asoe-ui) |
| S5 | P1.2 — silent refresh on case_* events | asoe-ui | WS handler invariants + 3 deleted describe blocks restored | IN_PROGRESS | `e81c962` (asoe-ui PR #148) |
| S6 | P1.3 — parameterised action-matrix coverage | asoe-ui | `describe.each(SCENARIOS)` for ExceptionDetailPanel | IN_PROGRESS | `35915a3` (asoe-ui PR #148) |
| S7 | P1.4 + P1.5 — email-source data presence + stats invariant | asoe-ui | Parameterised section dispatch + `MOCK_STATS` total invariant | IN_PROGRESS | `2a924a4` (asoe-ui PR #148) |
| S8 | P2.1 — mock `disposition` validates `reason_tag` (split write/read) | asoe-ui | Two-rule mock validator + parity test | IN_PROGRESS | `fe26506` (asoe-ui PR #148) |
| S9 | P2.2 — mock lifecycle/SoD/role gating | asoe-ui | Mock enforces terminal-state, initiator SoD, role gates | IN_PROGRESS | `52d10c8` (asoe-ui PR #148) |
| S10 | P2.3 — mock WS emits `case_*` events | asoe-ui | Mock state mutations fire `case_open`/`case_update`/`case_close` (Tier 1: event log; Tier 2 deferred) | IN_PROGRESS | `4fb4f09` (asoe-ui PR #148) |
| S11 | P2.4 — dual-mode CI + Prometheus counter | asoe-ui | `vitest.yml` workflow (mock = PR-blocking; stub = informational tier) + JUnit→Prometheus exporter emitting `tests_scenario_failed_total{scenario_id, slo_category, mode}`. Stub-mode formal gating deferred until `slo_category: stub_safe` opt-in. | IN_PROGRESS | `9178412` (asoe-ui PR #152) |
| S12 | P1.6 — fill scenarios to 12 intents | asoe-ui | Remaining 9 scenarios with curated reason tags | IN_PROGRESS | `7cd23b5` (asoe-ui PR #148) |
| S13 | ADR-034 §6.2 amendment | asoe2 | Decided: rename is **transitional** with deadline **2026-08-12**. Dual acceptance via `_MANUAL_EVENT_TYPES`, `ROUTABLE_EVENT_TYPES`, registry alias. Deprecation counter tracks the cutover. | IN_PROGRESS | `501cf6c` + `0fe7d3f` |
| S14 | P3 — vocabulary rename cleanup | asoe2 | `EmailOrderEntryRecipe.py` → `ManualOrderIntakeRecipe.py` with re-export, `EMAIL_ORDER_ENTRY_AUTONOMY_LEVELS` aliased, test file renamed, 27 new contract tests, deprecation counter wired to §28.6. | IN_PROGRESS | `b9c421f` |

**Status values:** `PENDING`, `IN_PROGRESS`, `BLOCKED:<reason>`, `MERGED`, `SKIPPED:<reason>`.

---

## 2. Pre-flight checklist (every session)

Before doing any work in a session, the agent must:

1. `git fetch origin` in both repos.
2. Read this file (`docs/plans/gap-remediation-rollout.md`) and find the first row whose status is `PENDING`. That row is the session's scope. **Do not skip rows** — phases have ordering invariants. If the row is `BLOCKED:<reason>`, halt.
3. Read the gap report and reviewer panel from the transcript history. The reviewer panel's three amendments are non-negotiable.
4. Run the baseline test suites in the repo(s) the session touches and record pass/fail counts:
   - `cd /home/user/asoe-ui && npm ci && npx vitest run --reporter=basic 2>&1 | tail -20`
   - `cd /home/user/asoe2 && uv sync && pytest -q 2>&1 | tail -20`
   If `npm ci` or `uv sync` is unavailable in the environment, document that in the post-flight summary and rely on CI for verification.
5. Confirm the working tree is on `claude/analyze-asoe-gaps-0LzG5` in each repo touched. No branch switching during a session.

If any pre-flight step fails, halt and ask. Do not proceed.

---

## 3. Post-flight checklist (every session)

After the session's scope is complete:

1. All new tests pass locally (or, if the local environment lacks deps, the postlude summary documents what CI will verify).
2. No existing test is regressed (compare to baseline).
3. Commit with a Conventional-Commits message that names the phase: e.g. `feat(p0): align reason_tag casing with curated vocabulary (S1)`. One commit per session unless the scope truly needs splitting.
4. `git push -u origin claude/analyze-asoe-gaps-0LzG5` with the standard exponential-backoff retry loop.
5. If this is the first commit on the asoe-ui side of `claude/analyze-asoe-gaps-0LzG5`, open a draft PR mirroring asoe2's PR #144. Otherwise no new PR — the stacked commit appears in the existing PR automatically.
6. Update this status table in the same session: set the row's `Status` to `MERGED` (post-merge — only the human can do this) or `IN_PROGRESS` (PR open) or `BLOCKED:<reason>`. Fill the `Commit` column with the short SHA. Commit the status update on top of the work commit in the same session.
7. Stop. Do not pick up the next session — that is a new invocation.

---

## 4. Emergency-stop conditions

Halt and surface a question if any of the following happen:

- A test in the baseline (step 5 of pre-flight) regresses unexpectedly during your session — root-cause first, don't paper over.
- A reviewer-panel amendment cannot be honoured as specified (e.g. the asoe2 `_CURATED_INTENT_REASON_TAGS` has moved). Re-derive in writing before continuing.
- The phase touches `compliance/`, `db/migrations/`, or `contracts/models.py`. These have explicit human review requirements in CODEOWNERS — open the PR but do **not** mark ready-for-review without a human sign-off note in the PR body.
- A backend route signature in `api/routes/exceptions.py` would have to change to satisfy a UI expectation. The backend contract is binding; the UI must adapt, not the other way round, unless an ADR amendment is filed first.
- Cross-repo coordination is needed but only one repo has been updated — never leave the contract dangling across a session boundary.

---

## 5. Phase specs

Each phase is one autonomous session. Phases are self-contained — none depend on the next being landed.

### S1 — P0.1 `reason_tag` casing fix + first 3 scenarios

**Repo:** `asoe-ui`
**Branch:** `claude/p0-reason-tag-casing`

**Files to add:**
- `tests/scenarios/_types.ts` — `BehaviourScenario` interface (see §A1 below for shape).
- `tests/scenarios/index.ts` — re-export array of all scenarios; `slo_category` tagging.
- `tests/scenarios/duplicate_po__high_value_needs_cosign.ts`
- `tests/scenarios/manual_order_intake__low_confidence_clarify_buyer.ts`
- `tests/scenarios/back_order__failed_terminal.ts`
- `tests/contract/snapshots/curated_reason_tags.json` — committed snapshot, generated from asoe2.
- `scripts/sync-reason-tag-snapshot.ts` — Node script that reads `../asoe2/constraints/specs.py` and writes the JSON snapshot. Errors loudly if the source file is absent.
- `tests/contract/test_reason_tag_vocab_parity.test.ts` — asserts `MOCK_HEALTH.allowed_override_reason_tags_by_intent` equals the snapshot byte-for-byte.

**Files to edit:**
- `src/hooks/useExceptionActions.ts:97,119` — replace hardcoded `reason_tag: "other"` with `pickQuickActionReasonTag(detail.intent, health)`. The helper lives in `src/lib/cases.ts`. When `health` is `undefined` (offline), do **not** fall back to a literal — disable the action and surface a toast: `"Health unavailable — actions disabled. Retry in a moment."` (Architect + Compliance amendment.)
- `src/lib/cases.ts` — add `pickQuickActionReasonTag(intent, health): string | null`. Returns the curated `OTHER`-equivalent sentinel for the intent (looks for `"OTHER"`, then case-insensitive `"other"` for grandfathered intents), or `null` when health is offline. **No casing transform.**
- `src/lib/api.ts:1215` — replace static lowercase array with `IMPORTED_CURATED_VOCAB.allowed_override_reason_tags_by_intent`, imported from `src/lib/__generated__/curated_reason_tags.ts` (generated from the snapshot).
- `tests/fixtures.ts:160-189` — same replacement.
- `.gitignore` — add `src/lib/__generated__/`.
- `package.json` — add `"generate:scenarios": "tsx scripts/generate-curated-vocab.ts"` and wire into `prebuild` + `pretest`.

**Tests to add:**
- `tests/architectural/quick_actions_reason_tag.test.ts` — read `useExceptionActions.ts`, assert no string literal matching `/reason_tag\s*:\s*["'](other|OTHER)["']/` survives.
- `tests/contract/test_reason_tag_vocab_parity.test.ts` (above).
- `tests/components/QuickActions.offline.test.tsx` — when `useHealth` returns `null`, Approve/Reject buttons render disabled with an aria-disabled toast on click.

**Exit criteria:**
- `npx vitest run tests/architectural/quick_actions_reason_tag.test.ts tests/contract/test_reason_tag_vocab_parity.test.ts tests/components/QuickActions.offline.test.tsx` is green.
- `npm run typecheck` is green.
- No existing test regresses.
- Snapshot is committed; running `npm run generate:scenarios` on a clean checkout produces a byte-identical snapshot.

---

### S2 — P1.0 failing CSA one-task test (PO amendment)

**Repo:** `asoe-ui`
**Branch:** `claude/p1-csa-one-task-failing`

**Files to add:**
- `tests/contract/test_csa_one_task_flow.test.tsx` — `it.skip("CSA reaches action ribbon in one click from /cases/{id}", …)`. Body **describes** the expected one-task flow per ADR-034 §6.1 + PO #133. Skip mark links to a tracking issue.
- `docs/test-strategy/csa-one-task-tracking.md` — one-pager: the gap, the PO's framing, the deferral rationale, the exit criterion for un-skipping.

**Exit criteria:**
- The test runs as `skip` (does not block CI).
- `grep -rn "csa-one-task" docs/` returns the tracking doc — searchable.
- PR body explicitly names the deferral in its `## Why this is a skip` section.

---

### S3 — P2.0 mirror asoe2 stub conformance into asoe-ui mock

**Repo:** `asoe-ui` (reads from `asoe2`)
**Branch:** `claude/p2-mock-conformance-mirror`

**Files to add:**
- `tests/contract/test_mock_stub_conformance.test.ts` — mirrors every invariant from asoe2's `tests/contract/test_stub_schema_conformance.py`. For each backend route the UI calls, assert that the UI mock's response shape matches the asoe2 stub's response shape exactly (key set, value types, enum constraints).
- `scripts/extract-stub-schemas.py` — reads asoe2's OpenAPI spec at `openapi/` and writes `tests/contract/snapshots/stub_schemas.json`.
- Wire into `package.json` as `generate:stub-schemas`.

**Files to edit:**
- `tests/scenarios/_types.ts` — add `slo_category: "p99_blocker" | "p95_blocker" | "background"` field.
- The 3 scenarios from S1 — annotate.
- `.github/workflows/tests.yml` — emit `tests_scenario_failed_total{scenario_id, slo_category}` as a Prometheus counter on failure. Read from JUnit XML output.

**Exit criteria:**
- Every URL the UI's `src/lib/api.ts` calls has a corresponding entry in the stub schema snapshot.
- The conformance test fails loudly if a URL is unmocked or shape-mismatched.
- `slo_category: p99_blocker` tests run on every PR; `background` tagged tests can be gated to nightly via env.

---

### S4 — P1.1 cursor pagination on case-projected queue

**Repo:** `asoe-ui`
**Branch:** `claude/p1-cursor-pagination`

**Files to edit:**
- `src/hooks/useManualOrderCases.ts` — add `{ cursor?: string; limit?: number }`. Internal `do { ... } while (cursor)` loop; accumulates `items` until `has_more === false`. Match the May-7 semantics exactly.
- `src/lib/api.ts` — `casesApi.list` mock: slice `MOCK_CASES` by `cursor` (use array index as opaque cursor), return `has_more`/`cursor` correctly.

**Files to add:**
- `tests/architectural/cursor_pagination_case_projected.test.ts` — port the deleted May-7 describe block to the new hook. Asserts the `do/while` shape in `useManualOrderCases.ts` source via static read (mirrors `detail_error_surfacing.test.ts` style).
- `tests/hooks/useCases.cursor.test.ts` — unit: seed mock with 3 synthetic pages, assert the hook accumulates all rows.
- `tests/browser/exception-queue-pagination.spec.ts` — Playwright leg.

**Exit criteria:**
- The 3 tests pass.
- `CaseListPane.tsx`'s `§D7 pagination deferred` comment is replaced with a one-line link to the test file.
- Mock `casesApi.list` emits realistic `has_more`/`cursor` on every call.

---

### S5 — P1.2 silent refresh on case_* events

**Repo:** `asoe-ui`
**Branch:** `claude/p1-case-ws-silent-refresh`

**Files to add:**
- `tests/architectural/case_invalidation_silent_refresh.test.ts` — three describe blocks, replacing the May-7 deleted invariants:
  1. `case_update` → `useCases.refetch()` called; `loading` stays `false`.
  2. `case_open` → list refetch + detail refetch when the new case's first record is the selected exception.
  3. `case_close` → terminal status visible within one event-loop tick.

**Files to edit:** (minimal — just enough to make the tests pass)
- `src/app/exceptions/page.tsx` — verify the existing WS handler passes `refetch` correctly. If not, fix.

**Exit criteria:** all 3 tests pass; no spinner flash assertion regresses.

---

### S6 — P1.3 parameterised action-matrix coverage

**Repo:** `asoe-ui`
**Branch:** `claude/p1-action-matrix`

**Files to add:**
- `tests/components/ExceptionDetailPanel.actions.test.tsx` — `describe.each(SCENARIOS)` loop. For each scenario, asserts every action's button visibility, enable state, disposition payload shape, and reason-tag dropdown contents.
- `tests/components/ExceptionDetailPanel.matrix.test.tsx` — full Cartesian (12 intents × 4 lifecycle states × 3 roles), gated by `RUN_FULL_MATRIX=1` env var. Default: skipped in CI; nightly only.

**Exit criteria:**
- 3-scenario matrix passes.
- Full-matrix test runs and passes when invoked manually.
- Snapshot output is human-readable (use `it("$scenario.id — $action", …)`, not auto-numbered).

---

### S7 — P1.4 + P1.5 email-source data presence + stats invariant

**Repo:** `asoe-ui`
**Branch:** `claude/p1-data-presence-stats`

**Files to edit:**
- `tests/architectural/email_source_section_data_presence.test.ts` — parameterise over `scenario.email_source ∈ {undefined, EmailSourceData}`.
- `tests/architectural/stats_lifecycle_totals_invariant.test.ts` (add) — assert `MOCK_STATS.by_lifecycle_state.EXECUTING === undefined` and `Σ(values) === total`.

**Exit criteria:** tests pass; invariant locked.

---

### S8 — P2.1 mock disposition validates `reason_tag` (split write/read)

**Repo:** `asoe-ui`
**Branch:** `claude/p2-mock-disposition-validation`

**Files to add/edit:**
- `src/lib/api.ts` — mock `PATCH /exceptions/{id}/disposition`:
  - On write: enforce curated vocab per intent (mirror `is_valid_reason_tag_for_write`).
  - 422 response shape matches live: `{ error: { code: "INVALID_REASON_TAG", message: "..." } }`.
  - **Never auto-uppercase** (Compliance amendment).
- `src/lib/api.ts` — mock `GET /exceptions/{id}/trace` (read): accept grandfathered lowercase tags in historical audit-log rows; expose via the trace payload.
- `tests/contract/test_mock_disposition_validation.test.ts` — parity test against asoe2's `tests/test_override_escalate.py` cases. Same status codes, same error payloads.

**Exit criteria:** mock rejects bad payloads with 422 in the live shape; parity test green.

---

### S9 — P2.2 mock lifecycle / SoD / role gating

**Repo:** `asoe-ui`
**Branch:** `claude/p2-mock-lifecycle-gating`

**Files to edit:** `src/lib/api.ts` mock handlers — enforce:
- Terminal-status records reject `disposition`.
- Cosign rejects when caller is the initiator (SoD).
- Reanalyze rejects past `MOCK_REANALYSIS_MAX_ATTEMPTS`.
- Role-gating per `useAuth().permissions`.

**Tests added:** parity tests against `asoe2/tests/test_override_escalate.py::TestOverrideEligibilityMatrix`.

**Exit criteria:** every Python eligibility-matrix case has a JS twin returning the same HTTP status.

---

### S10 — P2.3 mock WS emits `case_*` events

**Repo:** `asoe-ui`
**Branch:** `claude/p2-mock-ws-case-events`

**Scope:** when a mock disposition/escalate/reanalyze succeeds, the mock WS server emits the corresponding `case_open`/`case_update`/`case_close` event with the scenario-derived `intent`/`status`. Wires S5's silent-refresh tests to a realistic event source.

**Exit criteria:** running the Playwright spec without manual event injection still triggers the silent-refresh path.

---

### S11 — P2.4 dual-mode CI + Prometheus counter

**Repo:** both
**Branch:** `claude/p2-dual-mode-ci`

**Scope:**
- Vitest config switch: `ASOE_API_MODE=mock` (current) and `ASOE_API_MODE=stub` (against a local asoe2 container).
- `.github/workflows/tests.yml`: run both modes, emit `tests_scenario_failed_total{scenario_id, slo_category, mode}`.
- asoe2 side: a tiny `docker-compose.stub.yml` profile that runs the API in `ASOE_API_MODE=stub` for the asoe-ui dual-mode runner to dial into.

**Exit criteria:** PR CI matrix shows two columns (`mock`, `stub`); both green; Prometheus counter visible in the §28.6 Grafana dashboard.

---

### S12 — P1.6 fill remaining 9 scenarios

**Repo:** `asoe-ui`
**Branch:** `claude/p1-scenarios-twelve-intents`

**Scope:** one file per remaining intent in `tests/scenarios/`. Mechanical — copy the template; fill curated reason-tags from the snapshot; pick one operator-relevant scenario per intent (input from Domain SME panel notes preferred).

**Exit criteria:** `SCENARIOS.length === 12`; matrix test from S6 passes for all 12.

---

### S13 — P3 vocabulary rename completion

**Repo:** `asoe2`
**Branch:** `claude/p3-rename-completion`

**Scope:**
- Rename `recipes/EmailOrderEntryRecipe.py` → `recipes/ManualOrderIntakeRecipe.py` with a stub re-export at the old path for one release cycle.
- Rename `EMAIL_ORDER_ENTRY_AUTONOMY_LEVELS` → `MANUAL_ORDER_INTAKE_AUTONOMY_LEVELS`.
- Rename `tests/test_e2e_email_order_entry.py` → `tests/test_e2e_manual_order_intake.py`; update module docstring.
- `event_type="EMAIL_ORDER_ENTRY_REQUEST"`: file an ADR-034 §6.2 amendment first — is this name permanent (legacy producer compat) or transitional? Do not rename until ADR is amended.

**Exit criteria:** all renames green; ADR amendment merged or filed; old paths kept behind deprecation stubs for one release.

---

## 6. PR body template

```markdown
## Phase: <Sx — Pn.m short description>

Refers to: docs/plans/gap-remediation-rollout.md row <Sx>

## What changed
- <one bullet per file/scope>

## Reviewer-panel amendments honoured
- Architect: <evidence>
- Compliance: <evidence>
- Frontend Platform: <evidence>
- (omit lenses that don't apply to this phase)

## Tests
- Added: <list>
- Now green: <count>
- Baseline regression: <count, must be 0>

## Why this is a skip (only for S2)
<paragraph>

## Not in scope
<paragraph; references future phase>

## How to verify
<commands>

🤖 Autonomous run — please review.
```

---

## 7. Cross-cutting safety rails

- **Never** push to `main` directly. Every phase is a draft PR.
- **Never** skip pre-flight or post-flight. The status table is the only source of truth across sessions.
- **Never** rename a public symbol (export, route, env var) outside the phase that is explicitly authorised for it (S13).
- **Always** test against `main` baseline; if `main` itself is broken, halt and surface — never paper over.
- **Always** read the gap report and reviewer panel before starting, even if you ran the previous session. Context resets between sessions.

---

## Appendix A — `BehaviourScenario` shape (S1 deliverable)

```ts
// tests/scenarios/_types.ts
import type {
  AllowedIntent,
  OrderCase,
  ExceptionSummary,
  ExceptionDetailResponse,
  EmailSourceData,
  WSEvent,
} from "@/types";

export type SloCategory = "p99_blocker" | "p95_blocker" | "background";

export interface BehaviourScenario {
  id: string;                       // "DUPLICATE_PO__high_value_needs_cosign"
  slo_category: SloCategory;
  customer_visible_urgency: "real_time" | "same_business_day" | "batch";
  intent: AllowedIntent;
  case: OrderCase;
  records: ExceptionSummary[];      // cursor-pageable
  detail: ExceptionDetailResponse;  // for /exceptions/[id]
  email_source?: EmailSourceData;   // ADR-034 Phase G
  allowed_actions: {
    approve:   { allowed: boolean; reason_tag: string };
    reject:    { allowed: boolean; reason_tag: string };
    override:  { allowed: boolean; reason_tags: readonly string[] };
    escalate:  { allowed: boolean };
    reanalyze: { allowed: boolean };
    cosign:    { allowed: boolean };
  };
  expected_ws_events: readonly WSEvent[];
  learning_signals?: {
    earned_from_anchor_example?: string;  // ADR-039 X.1
    l2_shadow_disagreed?: boolean;
    operator_override_confirmed_by_buyer?: boolean;
  };
  tool_call_sequence?: readonly string[]; // ADR-038 §6 Case Agent tool surface
  tier?: 1 | 2 | 3;                       // ADR-038 §7 materialisation
}
```

---

## Appendix B — Session cadence assumptions

- Each session is one autonomous agent invocation, ~1–3 hours of work, single repo (S11 is the cross-repo exception).
- Sessions are independent: a failed PR review on S4 does not block S5.
- Human reviewer turnaround (`@kumarabhijit`) is the rate-limiting step. Agent posts PR as draft, surfaces in chat, stops.
- If a phase exceeds 3 hours mid-session, split: commit what's working, mark status `IN_PROGRESS:partial`, surface a question.

---

## Appendix C — What this plan does not do

- Does not rebuild `/inbox` (PO guidance, deferred).
- Does not mount HITL actions or PipelineDAG on `/cases/[id]` in P1 (PO ratified `/exceptions/[id]` as the canonical action surface; reviewed via S2 failing test).
- Does not change backend route signatures.
- Does not introduce new product features.
- Does not modify `compliance/` registries without explicit human sign-off.
