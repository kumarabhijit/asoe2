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
- **Phase 3 — extraction read path** ✓ — `OrderEntryExtraction` schema +
  composer, `OrderEntrySection` UI projector, email-content sanitizer
  (`llm.sanitizer.sanitize_email_text_for_llm`).
- **Phase 3 — extraction gateway core** ✓ (2026-05-24): `OrderExtractionGateway`
  (`gateways/extraction.py`) + `RecordedGatewayBackend` replay seam
  (`gateways/recorded_backend.py`) + recorded fixtures
  (`tests/fixtures/gateway/order_extraction/*.recorded.json`, provenance via
  `scripts/record_gateway.py`; live `OutlinesExtractionBackend` import-guarded).
  Constrained-gen contract `ExtractedOrderEnvelope` reuses `api.schemas`
  submodels (no drift). DoR #1 sanitizer wired into the gateway. Red-green never
  hits a live model.
- **Gateway fan-out pool decoupled** ✓ — `resolve_dependencies` fans out on its
  own pool (`_DEPENDENCY_FANOUT_POOL`), not `GatewayExecutor._pool` (fixed the
  recursive-submit self-deadlock; a recipe's dep count is no longer pool-bounded).
- **Phase 3 — producers wired live** ✓ — `ManualOrderIntakeRecipe` declares the
  `order_extraction` (extract_order + extract_entities) + `sap_order` (validate)
  read producers (`required_for_audit=False`); stubs in `api/sandbox_gateways.py`
  + `tests/conftest.py`. The Order Entry / Entities / SAP Data tabs now activate
  end-to-end (`enrichment_context` → composer). Preview-only when a producer
  isn't wired (empty bag → tab omitted).
- **Phase 3 — health-autonomy gate → green** ✓ — `/health` serves
  `autonomy_vocab_version` + ranked `allowed_autonomy_levels` ({level,label,rank});
  asoe-ui regen'd types + drives autonomy labels/ordering from health by `rank`
  (Guardrail #1), hardcoded map demoted to transition fallback. **Display only —
  policy.py gating ladder NOT migrated (that coherent v2 flip stays separately
  gated).**
- **Phase 3 — ERP-submit safety foundation** ✓:
  - DoR #1 — email/attachment sanitizer wired into the extraction gateway.
  - DoR #2 — order intake never auto-executes; a one-click-approve intake routes
    to `MANUAL_REVIEW_REQUIRED` (the ERP submit is operator-gated). Guard in
    `execute_recipe` keyed on `intent == MANUAL_ORDER_INTAKE`.
  - DoR #3 — four-eyes cosign materiality from the SAP re-price
    (`_cosign_materiality_usd`, reads `sap_data.order_value_usd`), not the LLM
    `financial_impact_usd`.
- **Phase 3 — SubmitToErpRecipe (deterministic core)** ✓ —
  `recipes/SubmitToErpRecipe.build_erp_submission` builds the SAP
  sales-order-create payload from the reviewed order, applies operator
  corrections with a before/after audit, validates submittability (pure;
  REJECTED on empty/invalid). **Not yet registered/graph-wired** — see PENDING.

## PENDING
### Phase 3 (resume here) — ERP-submit execution wiring (graph re-entry)
Owner chose **graph re-entry** for the financial write (not inline-in-handler).
Remaining, in order:
1. **Register + wire SubmitToErpRecipe**: add `"SubmitToErpRecipe.py"` to
   `constraints/specs.py::AllowedRecipeName` + `recipes/registry.py::REGISTRY`
   (allowed_intents `MANUAL_ORDER_INTAKE`; declare an **ERP gateway effect** —
   e.g. `erp`/`create_sales_order`); register the `erp` StubGateway in
   `api/sandbox_gateways.py` + `tests/conftest.py` (the
   `test_gateway_registry_coverage` gate enforces both sites); update
   `test_registry.py` len/▷recipe-count assertions.
2. **Directed graph re-entry**: add a `directed_recipe` (and corrections-carrying)
   field to `GraphState`; `select_recipe` uses it when set (bypass
   `propose_recipe`); `validate_types` builds the `SubmitToErpRecipe` invocation
   from `enrichment_context["order_entry_extraction"]` + operator corrections.
3. **DoR #2 guard exemption**: the `execute_recipe` COMPLETE→MANUAL_REVIEW guard
   must key on the **classifier** recipe (`selected_recipe ==
   "ManualOrderIntakeRecipe.py"`), so the operator-authorised
   `SubmitToErpRecipe` is allowed to reach COMPLETE.
4. **Disposition trigger + cosign-on-approve**: on operator APPROVE of an intake
   record, the disposition handler re-enters the graph with
   `directed_recipe=SubmitToErpRecipe` (Shadow → execute → apply_effects = ERP
   write). When `_cosign_materiality_usd >= $10k`, stage `PENDING_COSIGN` first;
   the submit runs on cosign-approve. Corrections ride as disposition params →
   before/after + actor + timestamp into the hash-chained audit (ADR-023);
   retraining export stays a separate, gated sink.
5. **Phase-3 completion audit** against ADR-042 §3 + this handoff.

### Later phases
- (unchanged — Phases 4–8 below.)

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
