# Customer Inbox Port — Converged TDD/BDD Test Strategy

**Status:** Ratified by the 2026-05-24 expert panel (joint convergence).
**Scope:** the ADR-042 effort (Customer Inbox → `/cases` EMAIL_ENTRY lens),
across asoe2 (pytest) and asoe-ui (vitest + Playwright).
**Operating mode:** AUTONOMOUS, STRICT TDD/BDD — **every test is written
first** (red → green → refactor). This document is the authority the
autonomous loop follows.

> Companion: `asoe-ui/docs/test-strategy/customer-inbox-tdd-strategy.md`
> (frontend specifics). Builds on both repos' `docs/test-strategy/README.md`.

---

## 1. Why this exists

The 11-expert review found the architecture sound but **not build-ready**:
the LLM steps have no quality measurement, "confidence" is uncalibrated yet
gates money, the financial threshold and injection surface are exploitable,
delivery isn't idempotent, and observability/audit are narrower than the
prose. Every one of those is a *test we must write first*. Convergence:
**the test suite is the spec; nothing ships until its red test exists.**

## 2. The test pyramid (maps onto existing layers — no new framework)

| Band | Home | Contents | ~Share |
|---|---|---|---|
| **Deterministic base (wide)** | asoe2 `tests/test_*` | section Pydantic validators, composer projections, EDI-850 pure builder, recipe logic, audit-registry coverage | ~55% |
| Frontend base | asoe-ui `tests/components`, `tests/hooks` | `useOrderExtraction` state machine, section data-presence | ~20% |
| **Mid** | asoe2 `tests/test_routes_*`, `tests/sandbox` | disposition+cosign handler, WS emission, graph paths via `DeterministicFallbackBackend` | ~12% |
| **Eval (parallel CI gate)** | asoe2 `tests/eval/` (NEW) | golden-dataset scoring for every LLM step (§4) | gate |
| **Journey cap (thin)** | asoe-ui `tests/browser/operator-journeys/` | BDD operator journeys (§5) | ~10% |
| **Tripwire (narrow)** | asoe-ui `tests/architectural/` | source-grep locks — **tripwires only** | ~3% |

**Fowler smell guard:** one architectural lock *per invariant class*, not one
per section. Add a grep lock only for (a) a deliverable another file mounts
or (b) a fix-encoded predicate a behavioural test can't reach. The
`cases_workspace_render_guard` pattern is the model; do not clone it per pane.

## 3. Red-green-refactor in autonomous mode

Per work item, **outside-in**, test-first:

1. **BDD journey (red)** — Playwright spec in `tests/browser/operator-journeys/`.
2. **Composer projection unit (red)** — `tests/test_analysis_composer.py` /
   `test_analysis_adapters_*`: assert `analysis.<section>` from a frozen
   `state.enrichment_context`.
3. **Deterministic recipe/builder unit (red)** — `tests/test_recipes.py` /
   new `test_edi_builder.py`, via `run_graph` + `DeterministicFallbackBackend`
   (no LLM): assert effect rows / lifecycle / cosign gate.
4. **OpenAPI↔TS round-trip (red)** — regen `openapi/asoe2.openapi.json`
   (`scripts/export_openapi.py`) + `npm run generate-types`; contract tests go
   green only when both sides land.
5. **Eval (parallel gate)** — golden cases added *before* the gateway exists.
6. Green minimal code → refactor under green.

### Spec tests committed ahead of implementation (CI hygiene)
A RED gate landed before its production code uses
`@pytest.mark.xfail(reason=..., strict=True)` (or module-level `pytestmark`).
While unimplemented the assertion fails → recorded as **xfail** → CI stays
green and honest ("spec pending"). The instant the code is implemented the
test passes → **strict XPASS → hard CI failure**, forcing a deliberate marker
removal. For gated work (autonomy-vocab flip, financial writes) that marker
removal is the checkpoint that coincides with human + compliance sign-off —
so the gate cannot be silently satisfied. Never delete the assertion to go
green; only remove the `xfail` when the implementation legitimately lands.

### The recorded-fixture boundary (how we TDD non-deterministic LLM steps)
Red-green **never** hits a live model. The fixture boundary is the *gateway
interface*, not the model.
- **Inputs:** extend `tests/fixtures/synthetic/*.event.json`.
- **Recorded gateway outputs:** `tests/fixtures/gateway/<gateway>/<case>.recorded.json`
  (constrained-generation output + `model_id` + `prompt_hash`), produced by a
  deliberate, reviewed `scripts/record_gateway.py` run — **never auto-refreshed
  in CI**.
- **Replay seam:** a `RecordedGatewayBackend` (sibling of the proven
  `DeterministicFallbackBackend`) replays the frozen output, so recipes and the
  composer are TDD'd deterministically: *given this extraction, the builder /
  constraint eval / composer must produce exactly X.* Constrained generation
  guarantees **shape**; the eval harness guarantees **correctness** — separately,
  nightly.

## 4. Eval harness (the #1 missing test-first artifact)

- **Location:** `tests/eval/`. Datasets: `tests/eval/datasets/<task>/*.jsonl`
  for `classification`, `extraction`, `draft_reply`, `compliance_shadow`.
- **Row:** `{id, input, expected, autonomy_vocab_version, labeler, model_pinned}`,
  human-curated.
- **Metrics:** classification → confusion matrix + macro-F1 + **ECE
  (calibration)** on `OrderAnalysis.confidence`; extraction → per-field
  accuracy + **hallucination rate** (field with no source span — the dollar
  metric); draft_reply → faithfulness/hallucination (human-facing, scored not
  shape-gated); compliance_shadow → agreement vs golden with **false-GREEN =
  zero tolerance**.
- **CI:** `pytest tests/eval -m replay` (deterministic, against
  `RecordedGatewayBackend`) is a **PR gate**; `pytest tests/eval -m live`
  (real model scorecard) runs **nightly** and fails nightly only. Thresholds
  in `tests/eval/thresholds.yaml`; lowering one requires the compliance
  CODEOWNERS gate. Every row + scorecard pins `model_id`, `prompt_hash`,
  `autonomy_vocab_version`.

## 5. BDD format

Operator journeys as Given/When/Then Playwright specs (sandbox backend; assert
against re-fetched backend state, never trusted UI state). Worked happy path:

```
// tests/browser/operator-journeys/email-entry-order-intake.spec.ts
GIVEN a MANAGER and a seeded EMAIL_ENTRY/NEW_ORDER case
WHEN  operator opens /cases, applies the EMAIL_ENTRY chip, opens the case
THEN  AgentReasoningCard shows classification + confidence (Layer 1)
WHEN  operator runs extraction and edits one line-item qty
THEN  OrderEntrySection reaches "done"; correction persisted via disposition
WHEN  operator submits to ERP (>$10k → cosign prompt)
THEN  a second approver cosigns; status flips; /cases/{id}/records + audit_trail
      confirm before/after + actor (re-fetched, not from UI state)
```

## 6. Definition of Ready — safety gates as failing tests

Each must be **red today, green when fixed**. Blockers are explicit. (Full
Given/When/Then in the panel record; summarised here.)

| # | Gate | File | Blocks |
|---|---|---|---|
| 1 | Email-body/attachment injection sanitizer (`sanitize_email_text_for_llm`) — current sanitizer covers `OrderEvent.metadata` only | `tests/test_llm_sanitizer.py` | **Phase 0 + Phase 3** |
| 2 | Injected sub-$10k GREEN order routes to `MANUAL_REVIEW_REQUIRED`, not auto-execute | `tests/test_e2e_manual_order_intake.py` | **Phase 3** |
| 3 | Cosign threshold computed from **SAP master-data re-price**, not LLM `financial_impact_usd` | `tests/test_routes_cases_cosign.py` | **Phase 3** |
| 4 | Confidence calibrated (ECE ≤ target); autonomy gate not driven by hardcoded `0.80/0.95/0.99` | `tests/test_confidence_calibration.py` | Phase 0 + nightly |
| 5 | Delivery idempotency (provider message-id) + end-to-end `correlation_id` | `tests/test_e2e_case_materialisation.py` | **Phase 3** |
| 6 | Outbox/compensation for ERP-submit-OK / reply-fail | `tests/test_executor.py` | **Phase 3** |
| 7 | ingest→terminal latency SLO histogram; `LLMCallTrace` carries tenant/case/recipe | `tests/test_metrics_endpoint.py` | nightly |
| 8 | Email + SAP gateway metering + circuit breaker (parity with LLM tier) | `tests/test_gateways.py` | **Phase 3** |
| 9 | Business/disposition-event hash chain (today only `policy_audit_log` is chained) | `tests/test_db_audit_chain.py` | **Phase 3** |
| 10 | XSS/CSP on rendered email fields + SSRF allowlist on attachment fetch | `asoe-ui/e2e/.../email-render-xss.spec.ts` + `tests/test_security.py` | **Phase 3** |
| 11 | Automation-bias metrics: override rate, Layer-2-open rate, decision dwell | `tests/test_reviewer_override_sli.py` | nightly |
| S | Sandbox-injected records cannot acquire a prod tenant or append to the prod chain | `tests/test_sandbox_routes.py` | Phase 0 |

## 7. Phase-0 test-first backlog (ordered; write these RED first)

1. `tests/test_autonomy_vocab_version.py` — dual-version equivalence **hard gate**
   (v1 `L1`=observe … v2 `L1`=most-autonomous; no historical row rewritten).
2. `tests/test_openapi_contract.py` — regenerated schema carries the new section
   models + autonomy fields.
3. asoe-ui `tests/architectural/type_contracts.test.ts` — per-type autonomy union
   (**EDI=L3, OrderEntry=L4** — not a blanket L1–L4 widen) mirrors `generated.ts`.
4. `tests/test_health_autonomy.py` — `/health.allowed_autonomy_levels` returns
   `{level,label,rank}[]`; UI "health-absent" fallback = block.
5. `tests/test_websocket.py` + asoe-ui `case_invalidation_silent_refresh.test.ts`
   — new WS event types flow through `isCaseInvalidationEvent`.
6. `tests/test_case_type_invariants.py` — resolve `INVOICE_QUERY` vs `OTHER`
   (Phase-0 decision; default: add `INVOICE_QUERY`).
7. `tests/eval/test_harness_skeleton.py` — eval harness loads a dataset, replays
   via `RecordedGatewayBackend`, emits confusion+ECE, reads `thresholds.yaml`.
8. `tests/test_sandbox_routes.py` — sandbox isolation sentinel (gate S).
9. `tests/test_llm_sanitizer.py` — untrusted-content sanitizer (gate 1).

## 8. CI gates

**PR (must be green to merge):** `pytest tests/` (deterministic, no live LLM);
`pytest tests/eval -m replay`; `test_openapi_contract.py`; asoe-ui `tsc
--noEmit`, `vitest run` (incl. type/autonomy/vocab locks), `npm run build`,
Playwright operator journeys + deliverable-completeness, axe sweep; bug-fix
regression-on-parent ritual; CODEOWNERS gate on `audit_bearing_registry.yaml`
+ `thresholds.yaml`. The **autonomy_vocab_version hard gate** is blocking and
keeps ADR-042 at *Proposed* until it + dual-control sign-off pass.

**Nightly:** `pytest tests/eval -m live` (field accuracy, hallucination, ECE,
confusion, zero-tolerance false-GREEN); visual-regression baselines; full
cross-repo browser suite.

## 9. Autonomous operating contract

While running autonomously the loop MUST:
- Write the failing test (red) **before** any implementation; show red, then green.
- Keep red-green off live models (recorded-fixture seam); live evals are nightly.
- **Stop and require human + compliance sign-off** before merging any change to:
  the autonomy vocabulary semantics (`policy.py`), any financial-write /
  ERP-submit / outbound-reply path, the audit chain, or `thresholds.yaml`.
  Tests for these are written autonomously; the production flip is gated.
- Checkpoint at each phase boundary with a red/green summary.
- Never weaken a gate to go green; fix the cause or halt (`FAIL_TO_HUMAN`).
