# ASOE Customer-Inbox Port — Session Handoff

Snapshot for resuming the AgenticOM "Customer Inbox" → ASOE case-architecture
port across **asoe2** (backend) and **asoe-ui** (frontend). Authoritative copy;
asoe-ui has a pointer to this file.

_Last updated: 2026-05-24._

## Repos / branches / PRs
- **asoe2** (Python / FastAPI / LangGraph) — branch `claude/gifted-darwin-NbqQo`,
  **draft PR #166** → main.
- **asoe-ui** (Next.js) — branch `claude/gifted-darwin-NbqQo`,
  **draft PR #185** → main.
- Develop on `claude/gifted-darwin-NbqQo` in both. GitHub via MCP tools only
  (scope: these two repos). PR activity is subscribed (CI failures wake the
  session). Check `git rev-parse --short HEAD` in each repo for the live SHA.

## Operating context (carry these over)
- **Pre-production project. Owner has compliance veto → human compliance
  sign-off is WAIVED.** Keep the *in-code* Compliance-Shadow / audit / cosign
  *mechanisms* intact (they are the system being built); just don't pause for a
  human approval step to implement/merge. Do not strip the Shadow/cosign
  machinery unless explicitly told.
- **Strict TDD/BDD, test-first.** Spec-ahead-of-impl tests use
  `@pytest.mark.xfail(strict=True)` (or the vitest equivalent) so CI stays
  green; removing the marker == the implementation landed.
- **Discipline:** after every push, **wait for that PR's CI to go green** before
  the next task (~10-min fallback poll if no event — the subscription fires only
  on *failures*). Run `tsc` / tests as the **last** step before pushing.
- **Per-phase audit:** before declaring a phase complete, audit artifacts
  against the plan (verify, don't assert); only then advance.
- **Local env:** `pip` works (PyPI reachable) — asoe2 deps installed via
  `pip install -e ".[dev]"`; **system Python is 3.11, CI uses 3.14** (`uv`
  cannot fetch 3.14 here — local runs on 3.11 are fine for these tests).
  asoe-ui `npm install` done (Node 22) → `vitest` / `tsc` run locally;
  **Playwright browser-e2e is verified only in CI**. The heavy `outlines`/
  `torch` extra is NOT installed → the extraction gateway's constrained-gen
  won't run locally; TDD it via a `RecordedGatewayBackend` replay (no live
  model). **cwd drifts between bash commands — always `cd` explicitly.**

## DONE (all green in CI)
- **Phase 0** ✓ (test-first gates + closure audit) · **Phase 1** ✓ (EMAIL_ENTRY
  lens: `case_type` filter end-to-end + chip) · **Phase 2** ✓ (Entities + SAP
  Data sections + AI-Analysis; audited).
- **Autonomy v2** ✓ — `contracts/autonomy.py` (prototype ordering
  L1=most-autonomous … L4=human; versioned, no historical reinterpretation).
- **Phase 3 partial:** `OrderEntryExtraction` schema + composer ✓,
  `OrderEntrySection` UI projector ✓, email-content sanitizer
  (`llm.sanitizer.sanitize_email_text_for_llm`) ✓.
- All four Customer-Inbox tabs render as data-presence projectors —
  **preview-only** until producers populate `enrichment_context`.

## PENDING
### Phase 3 (resume here)
1. **Extraction gateway core** — constrained-generation LLM (Guidance/Outlines)
   reading the sanitized email/attachments → writes `order_entry_extraction` +
   `inbox_entities` + `sap_data` into `enrichment_context` (the producer that
   activates the tabs). Build a `RecordedGatewayBackend` replay harness first;
   TDD against recorded fixtures (never a live model in red-green).
2. **SAP-read producer** (pipeline "SAP check" → `enrichment_context["sap_data"]`).
3. **ERP-submit recipe** via sandbox/stub gateway + **order-entry corrections**
   as dispositions on `/exceptions/{id}/disposition` (NOT new `/cases` verbs).
4. **health-autonomy gate → green:** add `allowed_autonomy_levels`
   (`{level,label,rank}`) to the health route + `HealthResponse`, regen UI
   types, drive UI ordering from health by `rank` (greens
   `tests/test_health_autonomy.py`). Bounded.
5. Phase-3 completion audit.

### Later phases
- **Phase 4:** Draft Reply + Simulate Inbound + live pipeline. WS new event
  types (`pipeline_step` / `reply_drafted` / `reply_sent`) through
  `isCaseInvalidationEvent` (re-homed gate #5).
- **Phase 5:** EDI 850 (`Edi850Document` schema + builder + section).
- **Phase 6:** Change Analysis (`ConstraintEvaluation` / `ConstraintCheck` /
  `ScenarioOption` / `ChangeDecision`; recipe-homed, variable cardinality).
- **Phase 7:** Constraint Graph + Knowledge Graph (`KnowledgeGraphPayload`;
  deferrable) + sandbox `/sandbox/simulate-inbound` injector isolation sentinel
  (re-homed gate #8).
- **Phase 8:** Hardening — full test pyramid, axe, contract snapshots,
  ADR-042 → **Accepted**.

### Gates still RED / not yet written
- `tests/test_inbox_gate_openapi_contract.py` (xfail) flips green only when all
  8 section schemas exist (have `OrderEntryExtraction`; need the other 7 across
  Phases 4–7).
- DoR safety gates (strategy doc §6) — only **sanitizer** + **autonomy** are
  implemented; the rest are documented-but-unwritten and land in their phases:
  injected-GREEN → `MANUAL_REVIEW` (#2), cosign threshold from SAP master-data
  **re-price not LLM values** (#3), confidence **calibration/ECE** (#4),
  delivery **idempotency + correlation_id** (#5), **outbox/compensation** (#6),
  ingest→terminal **SLO histogram** (#7), email/SAP gateway **circuit breaker**
  (#8), **business/disposition audit hash chain** (#9), **XSS/CSP + SSRF** (#10),
  **automation-bias metrics** (#11).

## Documents to refer to
**asoe2**
- `docs/adr/ADR-042-customer-inbox-prototype-port.md` — master plan/decisions
  (status *Proposed*).
- `docs/test-strategy/customer-inbox-tdd-strategy.md` — TDD/BDD strategy, DoR
  gate table (§6), Phase-0 closure (§7).
- Background ADRs: 041 (case_type axis / `/cases` workspace), 038 (case-centric),
  034 (email-order-entry), 025 (gateway-before-shadow), 031 (read-projection
  split), 027 (pipeline viz), 039 (LLM shadow), 040 (cosign).
- Key code: `api/schemas.py` (`*AnalysisData` + `OrderEntryExtraction`),
  `api/profile_composer.py` (the `compose_*` cross-cutting projections — sole
  source for the inbox sections), `api/routes/exceptions.py` (`AnalysisResponse`
  assembly), `contracts/autonomy.py`, `contracts/policy.py` (autonomy v1 +
  thresholds), `llm/sanitizer.py`, `gateways/` (framework),
  `recipes/ManualOrderIntakeRecipe.py` (canonical; `EmailOrderEntryRecipe.py`
  is a legacy alias), `compliance/audit_bearing_registry.yaml` (every
  `*AnalysisData` field needs a tiered row + update the `summary` tally — guarded
  by `tests/test_audit_registry_coverage.py`), `scripts/export_openapi.py`.
- Reference tests: `tests/test_inbox_section_composers.py`, `tests/eval/`
  (harness + metrics), `tests/test_constraints.py` (constrained-gen lock style).

**asoe-ui**
- `docs/test-strategy/customer-inbox-tdd-strategy.md` (frontend half),
  `docs/prototype_gap_analysis.md` (§4 / §9 — predates ADR-041; the
  lens-on-`/cases` framing supersedes "enhance /inbox").
- Section pattern to mirror: `src/app/exceptions/EntitiesSection.tsx` /
  `SapDataSection.tsx` / `OrderEntrySection.tsx` + their mount in
  `src/app/exceptions/ExceptionDetailPanel.tsx` (data-presence guard) + tests in
  `tests/components/*Section.test.tsx` and
  `tests/architectural/*_section_data_presence.test.ts`.
- `src/types/exceptions.ts` (hand-written mirror — must match
  `src/types/generated.ts`; regen via `npm run generate-types` from
  `asoe2/openapi/asoe2.openapi.json`), `src/lib/cases.ts`,
  `src/hooks/useManualOrderCases.ts` (`useCases`), `src/app/cases/page.tsx`
  (lens chip).
- Guardrails (`asoe-ui/CLAUDE.md`): dumb projectors via `<EvidenceBlock>` (no
  `?? "—"`), no hardcoded enum literals (use `useHealth`), design tokens only,
  rich `*AnalysisData` types are a product commitment. Avoid `**/` inside JSDoc
  (oxc parser trap).

## Suggested first prompt for the fresh session
> Continue the ASOE Customer-Inbox port on branch `claude/gifted-darwin-NbqQo`
> (asoe2 PR #166, asoe-ui PR #185). Read `asoe2/HANDOFF.md`, then
> `asoe2/docs/adr/ADR-042-customer-inbox-prototype-port.md` and both
> `docs/test-strategy/customer-inbox-tdd-strategy.md`. Resume **Phase 3 =
> extraction gateway core**, test-first via a `RecordedGatewayBackend` replay
> harness (no live model). Pre-prod: compliance sign-off waived, keep in-code
> Shadow/audit intact. Discipline: TDD, run tsc/tests before push, wait for CI
> green (10-min fallback) between tasks, audit each phase against the plan
> before declaring complete.
