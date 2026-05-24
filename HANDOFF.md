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
  REJECTED on empty/invalid).
- **Phase 3 — ERP submit executes on the deterministic graph path** ✓ —
  directed graph re-entry (`GraphState.directed_recipe` / `directed_corrections`;
  `select_recipe` honours it; `validate_types` builds the submit invocation from
  the reviewed extraction + corrections). SubmitToErpRecipe registered
  (`AllowedRecipeName` + `REGISTRY`) with an `erp`/`create_sales_order` gateway
  effect (stub in sandbox + conftest). `GatewayEffect.only_on_recipe_status`
  gates the write to a SUCCESS submit; DoR #2 guard exempts the authorised
  submit (keys on the classifier recipe).
- **Phase 3 — SUBMIT_TO_ERP disposition trigger + four-eyes** ✓ — explicit
  `SUBMIT_TO_ERP` resolution action (health-surfaced; UI label + types mirrored).
  Sub-$10k submits run immediately → RESOLVED + `resolution_data.erp_submission`
  (ERP doc + corrections audit); ≥$10k stage `PENDING_COSIGN` and run on
  `/override/cosign` approve (SoD enforced); REJECTED submits write nothing and
  stay in review. Corrections ride as a disposition param → before/after into
  the audit trail.

## Phase-3 completion audit (2026-05-24)
Audited against ADR-042 §3 (Phase 3 = Order Entry: extract gateway + recipe +
corrections + ERP submit; gated by deterministic recipe test + Shadow +
cosign>$10k) — **all met**:
- ✓ Extraction gateway (constrained-gen contract) + RecordedGatewayBackend
  replay; red-green never hits a live model.
- ✓ Tabs activate from `enrichment_context` (order_entry_extraction /
  inbox_entities / sap_data) via the wired producers.
- ✓ ERP-submit recipe (deterministic, pure) with a deterministic test path +
  graph re-entry test; **Shadow-gated** (re-entry runs shadow_audit before
  execute); **cosign>$10k** from the SAP re-price (DoR #3).
- ✓ Corrections carried as disposition params → recipe before/after audit.
- ✓ DoR #1 (sanitizer in the gateway), #2 (no auto-execute), #3 (re-price
  materiality) all green.
- ✓ health-autonomy gate green (both repos); autonomy v2 hard gate intact.
Full asoe2 suite green (3009 passed / 1 xfailed = the deferred 8-section OpenAPI
contract); asoe-ui tsc + 1183 vitest + build green. **Phase 3 (MLS) complete.**

Deferred to **Phase 8 hardening** (strategy §6 DoR table — "land in their
phases", not Phase-3 blockers for the MLS): #5 delivery idempotency +
correlation_id, #6 outbox/compensation, #8 gateway circuit breaker, #9
business/disposition hash chain, #10 XSS/CSP + SSRF.

## Phase-4 completion audit (2026-05-24)
Audited against ADR-042 §4 (Phase 4 = Draft Reply + Simulate Inbound + live
pipeline (WS); gated by Shadow + cosign; sandbox-isolation test) — **met for the
shipped scope**:
- ✓ **AI Draft Reply (gen/edit/approve/send)** — `ReplyDraftRecipe` (pure,
  deterministic template composition; operator subject/body edits with a
  before/after audit; REJECTs on no-recipient / unknown-template). Wired via
  directed graph re-entry (Shadow → recipe → apply_effects). Two operator
  actions: `DRAFT_REPLY` composes → `resolution_data.reply_draft` (record stays
  in review); `SEND_REPLY` (mode="send" → status READY_TO_SEND) fires the
  `buyer_notification` GatewayEffect gated `only_on_recipe_status` so a REJECTED
  compose never sends. Send recomposes deterministically → sent == reviewed.
  `resolution_data.reply_sent` is SENT only when the gateway delivered, else
  FAILED with reason (Guardrail #5). Both actions health-surfaced + UI label +
  ResolutionAction/enum-parity/MOCK_HEALTH mirrored.
- ✓ **Live pipeline (WS)** — `reply_drafted` / `reply_sent` event types +
  payloads + factories, emitted from the disposition handlers; UI WSEventType
  union + payload interfaces mirrored; `isCaseInvalidationEvent` invalidates the
  list on them (re-homed strategy **gate #5**). `pipeline_step` is covered by
  the pre-existing `pipeline_progress` (ADR-027) — no redundant type added.
- ✓ **Shadow-gated** — every reply action runs the directed re-entry through
  `shadow_audit` before execute/apply_effects.
- ✓ **Simulate Inbound (backend injector)** — already exists:
  `POST /_sandbox/seed/manual-order-intake` (sandbox.py) pushes a synthetic
  order event through the **real** pipeline via `_resolve_state`.
Full asoe2 suite green (3033 passed / 1 xfailed); asoe-ui tsc + 1184 vitest +
build green. **Phase 4 complete for shipped scope.**

Deferred (panel-noted, low value vs. cost): the sandbox-only **Simulate Inbound
UI modal** (backend injector already exists; a dev affordance). Cosign on
replies is intentionally **not** applied — a clarification email is not
financially binding; cosign remains the four-eyes control for the ERP write.
The `/sandbox/simulate-inbound` **isolation sentinel** stays re-homed to
**Phase 7** (strategy gate #8).

## Phase-5 completion audit (2026-05-24)
Audited against ADR-042 §5 / scorecard (Phase 5 = EDI 850 builder + section,
gated by pure-function unit tests) — **met**:
- ✓ **Deterministic builder** — `gateways/edi850.build_edi_850` is a pure,
  fully unit-testable port of the prototype `buildEDI850`: reconstructs the ANSI
  X12 5010 PO (ISA/GS/ST/BEG/CUR/REF/DTM/N1/PO1/PID/CTT/SE/GE/IEA). No I/O,
  clock, or randomness — control numbers derive from `order_id` (CRC32), so
  output is byte-stable (9 unit tests: structure, totals, CTT/SE counts,
  determinism, optional ship-to, priceless line, empty order).
- ✓ **Schema** — `Edi850Document` (+ envelope/header/party/line/totals/segment
  submodels) on `AnalysisResponse.edi_850_audit`; surfaced into OpenAPI
  (openapi-fresh CI check green). Carries the three prototype sub-views.
- ✓ **Composer** — `compose_edi_850_document` projects
  `enrichment_context["edi_850"]` (None when absent/malformed — preview-only,
  Guardrail #6). Builder owns construction; composer only projects.
- ✓ **Producer wired end-to-end** — `edi_850`/`build` read producer on
  ManualOrderIntakeRecipe (`required_for_audit=False`); StubGateways in sandbox
  + conftest build the canned order deterministically, so the EDI 850 Audit tab
  activates alongside Order Entry / SAP Data.
- ✓ **UI section** — `Edi850Section` (dumb projector) with Decoded / Raw X12
  (colour-coded + copy) / Segment Map sub-views; mounts behind the
  `edi_850_audit` data-presence guard; hand-written + generated types mirror the
  contract; deliverable lock + component test.
Full asoe2 suite green (3050 passed / 1 xfailed); asoe-ui tsc + 1192 vitest +
build green. **Phase 5 complete.**

The 8-section OpenAPI contract gate (`test_inbox_gate_openapi_contract.py`) now
has 2 of 8 schemas (`OrderEntryExtraction`, `Edi850Document`); stays xfail until
Phases 6–7 add the remaining 6.

## Phase-6 completion audit (2026-05-24)
Audited against ADR-042 §6 / scorecard (Phase 6 = Change Analysis,
variable-cardinality constraints, recipe-homed; gated by deterministic recipe
test + composer) — **met**:
- ✓ **Recipe-homed evaluator** — `recipes/ChangeAnalysisRecipe.evaluate_change`
  is pure + deterministic, scoring a requested order change against the
  constraint catalogue (Inventory/Production/Transport/Warehouse/Order Status/
  SLA/Financial/Dependencies/Network/Priority). **Variable cardinality** — only
  constraints whose backing signal is present evaluate (Financial always runs);
  N checks, not the prototype's fixed 10. NOT in `constraints/`, NOT in
  `agents/harness.py` (ADR §6 Architect correction). 10 deterministic unit tests.
- ✓ **Thresholds injected, not imported** — the four-eyes materiality threshold
  arrives as a `cosign_threshold_usd` param (caller reads
  `policy.HIGH_VALUE_OVERRIDE_THRESHOLD_USD`); the recipe stays policy-free
  (Invariant #11 — the recipe-policy-decoupling guardrails pass).
- ✓ **Scenarios + decision** — M scenarios (as-requested / partial / expedite /
  reject) derived deterministically; decision panel surfaces confidence,
  recommended action, revenue impact, SAP actions, and `requires_cosign` (gated
  on the injected threshold).
- ✓ **Schemas** — `ConstraintCheck` / `ConstraintEvaluation` / `ScenarioOption`
  / `ChangeDecision` (+ `ChangeItem` / `ChangeAnalysis` wrapper) on
  `AnalysisResponse.change_analysis`; surfaced into OpenAPI.
- ✓ **Composer + producer** — `compose_change_analysis` projects
  `enrichment_context["change_analysis"]` (None when absent/malformed,
  Guardrail #6); `change_analysis`/`evaluate` read producer on
  ManualOrderIntakeRecipe + sandbox/conftest stubs activate the tab end-to-end.
- ✓ **UI section** — `ChangeAnalysisSection` (dumb projector) renders the
  variable-cardinality constraints + scenarios + Layer-1 decision panel +
  lifecycle bar + change grid; mounts behind the `change_analysis` guard; types
  mirrored; deliverable lock + component test.
Full asoe2 suite green (3068 passed / 1 xfailed); asoe-ui tsc + 1199 vitest +
build green. **Phase 6 complete.**

## Phase-7 completion audit (2026-05-24)
Audited against ADR-042 §7 / §5b (Phase 7 = Constraint Graph + Knowledge Graph,
deferrable; + sandbox isolation sentinel) — **met**:
- ✓ **Knowledge Graph** — `gateways/knowledge_graph.build_knowledge_graph` is a
  pure, deterministic DERIVED projection of the case entities (order →
  customer / materials / SAP doc / extracted entities) into nodes+edges. No
  standalone KG source exists (§5b), so it derives from existing enrichment
  context, not invented data. `KnowledgeGraphNode/Edge/Payload` schema +
  composer (None when absent/empty — deferrable, Guardrail #6) +
  `knowledge_graph`/`build` read producer + stubs. `KnowledgeGraphSection`
  renders a deterministic radial SVG + an accessible relationships list (WCAG
  parity). 5 builder unit tests + composer + component + deliverable-lock tests.
- ✓ **DraftReply** (Phase-4 carry-over) — `DraftReply` (+ `DraftReplyEdit`)
  schema + `compose_draft_reply` (projects `resolution_data["reply_draft"]`,
  flattening the nested draft) + `DraftReplySection`. Completes the contract for
  the AI Draft Reply evidence on the analysis payload.
- ✓ **8-section OpenAPI contract gate GREEN** — all 8 ADR-042 §2.2.1 schemas now
  exported; `test_inbox_gate_openapi_contract.py` xfail marker **removed** — it
  is now a standing hard gate against section-schema removal.
- ✓ **Sandbox isolation sentinel (gate #8)** — `test_sandbox_inbound_isolation`:
  the inbound injector does not append to the prod audit hash-chain and its
  records are tenant-scoped. (The env-gate 403 was already locked in
  `tests/contract/test_sandbox_manual_order_intake_producer.py`.)
- ✓ **Registry** — ManualOrderIntakeRecipe now declares 6 inbox read producers
  (11 deps total); the registry lock asserts the full set.
Full asoe2 suite green (3088 passed, **0 xfailed** — the contract gate flipped);
asoe-ui tsc + 1213 vitest + build green. **Phase 7 complete.**

**Constraint Graph — deliberately deferred** (ADR §5b "deferrable behind
demand"; ADR §2.1 "reuse `get_pipeline_topology` + `/exceptions/{id}/trace`, do
NOT build a new surface"). It would duplicate the existing ADR-027 PipelineDAG /
trace and the Phase-6 ChangeAnalysisSection (which already renders the
constraint evaluation). No new graph surface built — the existing trace/topology
+ the Change Analysis section cover it.

## Phase-8 audit (2026-05-24) — hardening, tractable subset
Scoped (owner-approved) to the deterministic, low-risk, high-value gates; the
deep-infra gates are documented-deferred to productionization (they need new
instrumentation in the gateway/delivery/render core, not test-only work).

**Delivered + green:**
- ✓ **Field-level contract snapshot** — `test_inbox_section_contract_snapshot.py`
  pins the exact field set of all 8 SOX-evidence section schemas (a field move
  fails with a precise diff; complements the coarse openapi-drift test + the
  name-existence gate). Locks the asoe-ui type mirror in lockstep.
- ✓ **#4 confidence calibration** — `test_confidence_calibration.py`: the ECE
  scorer holds the `thresholds.yaml` budget (with teeth), and the autonomy bands
  are the named `policy.py` constants — AST guard forbids inline `0.95`/`0.99`
  in `orchestration/nodes.py`.
- ✓ **#7 ingest→terminal SLO histogram** — `asoe_ingest_to_terminal_latency_
  seconds` (Prometheus cumulative histogram) in `api/metrics.py`, surfaced via
  `render_all` on `/metrics`, fed from one contained never-raises timing point
  at `_resolve_state`. `test_slo_histogram.py`.
- ✓ **#9 business/disposition audit hash-chain** — already wired incrementally
  (12 chained `log_audit_event` call sites; tamper-evidence in
  `test_audit_chain.py`); Phase 8 adds `test_disposition_audit_chain_lock.py`, an
  AST guard that the ADR-042 disposition handlers keep chaining (no regression).
- ✓ **axe sweeps** — `tests/accessibility/inbox_sections_sweep.test.tsx` asserts
  all five Phase 3–7 sections are axe-clean in their canonical states.
Full asoe2 suite green (3106 passed); asoe-ui tsc + 1219 vitest + build green.

**Deferred to productionization (documented, NOT done):**
- #5 delivery idempotency (provider message-id) + end-to-end `correlation_id`,
  #6 outbox/compensation (ERP-OK / reply-fail), #8 email/SAP gateway
  circuit-breaker parity (LLM tier has one), #10 XSS/CSP headers + SSRF
  attachment allowlist — each needs new instrumentation in the gateway/delivery/
  render core (real infra + design tradeoffs), out of the low-risk subset.
- #11 automation-bias: override-rate SLI already exists
  (`shadow_llm_reviewer_override_rate_on_downgrades`); **Layer-2-open rate +
  decision dwell** need a UI→backend telemetry pipeline that does not exist yet.

**ADR-042 status stays *Proposed*** (NOT flipped to Accepted): per strategy §8
the `autonomy_vocab_version` hard gate keeps it Proposed until autonomy-v2
**dual-control compliance sign-off** — which is *waived-but-mechanism-intact* in
this pre-prod project, so flipping the status unilaterally would misrepresent the
compliance gate. Leave the flip to the sign-off step.

## Productionization audit (2026-05-24) — deferred-gate progress
Working the post-feature-port backlog. **Landed this pass:**
- ✓ **#8 gateway circuit breaker + metering** — per-`gateway_name`
  `LLMCircuitBreaker` at the single `GatewayExecutor.run` chokepoint (OPEN
  short-circuits to UNAVAILABLE without hitting the dependency; cooldown →
  HALF_OPEN probe → CLOSED). Per-gateway call/failure/short-circuit/latency
  metering + breaker-state gauge on `/metrics`. Thresholds in `policy.py`;
  process-local + reset per test (conftest). `tests/test_gateway_circuit_breaker.py`.
- ✓ **#10 XSS/CSP** — `SecurityHeadersMiddleware` (strict `default-src 'none'`
  CSP on the JSON API + nosniff / frame-DENY / no-referrer / CORP on every
  response incl. errors; docs paths get a Swagger-compatible CSP).
  `tests/test_security_headers.py`. UI: no `dangerouslySetInnerHTML` anywhere —
  a source lock + render-escaping tests (`<script>`/`<img onerror>` → inert)
  prove backend free text is escaped. **SSRF deferred** — no live
  attachment-fetch path exists to allowlist yet.
- ✓ **#5 delivery idempotency** — `correlation_id` was already covered by
  `TraceIDMiddleware` (X-Trace-ID end-to-end). Added delivery-level dedup on the
  reply-send: a deterministic key over (case, recipient, subject, body) — a
  provider-message-id analog — is ledgered on a successful send and stamped on
  `resolution_data.reply_sent`; a repeat SEND_REPLY composes (no send), derives
  the key, and short-circuits if already delivered (audit
  `EXCEPTION_REPLY_SEND_DEDUPED`). `tests/test_reply_send_idempotency.py`.
- ✓ **#6 outbox/compensation** — `orchestration/outbox.py`: `apply_effects`
  records every effect outcome (SUCCESS → committed; failure → needs
  compensation), surfaced via `pending_compensation()` until `mark_compensated()`.
  Never-raising; tenant-scoped; reset per test. `tests/test_effect_outbox.py`.
  (DB-backed outbox table + the auto-reconciliation **worker** are the
  remaining production follow-on; this is the substrate.)
- ✓ **#11 automation-bias SLIs** — `reviewer_layer2_open_rate` +
  `reviewer_decision_dwell_seconds` (+ counters) on `/metrics`, fed by
  `POST /api/v1/metrics/reviewer-activity`. UI: `CollapsibleSection` reports its
  first expand via `Layer2OpenContext`; `ExceptionDetailPanel` tracks dwell +
  layer2-open and reports once per decision. `tests/test_reviewer_activity_metrics.py`
  + asoe-ui `collapsible_layer2_signal` / `report_reviewer_activity` tests.
  (Override-rate SLI already existed.)

### Infra-residue pass (2026-05-24)
- ✓ **#6 reconciliation worker** — `orchestration.outbox.reconcile_pending`
  re-runs pending failed effects via the executor (retry is delivery-idempotent),
  marks compensated on success, escalates after `max_attempts`. Admin trigger
  `POST /api/v1/outbox/reconcile`. `tests/test_effect_outbox.py`.
- ✓ **#10 SSRF guard + live wiring** — `hardening/ssrf.py::validate_outbound_url`
  (allowlist-first, HTTPS-only, default-port, no creds, blocks non-global /
  metadata / loopback / localhost / private + DNS-rebinding). **Wired** into
  `gateways/attachment_fetch.AttachmentFetchGateway`: every attachment URL is
  validated BEFORE retrieval (allowlist `policy.ATTACHMENT_FETCH_ALLOWED_HOSTS`);
  blocked URLs return FAILED and never reach the (injectable) fetcher. Registered
  in sandbox + conftest. `tests/test_ssrf_allowlist.py` + `test_attachment_fetch_gateway.py`.
- ✓ **Validation mock data (asoe-ui)** — `src/lib/mock-data/inbox-sections.ts`
  populates every inbox section in mock mode. **8 EMAIL_ENTRY cases**: enriched
  exc-026 (new order — all sections); exc-040..043 change requests (qty
  reduction / expedite / cancellation / SKU substitution → Change Analysis +
  graph); exc-044 INQUIRY, exc-045 COMPLAINT, exc-046 happy-path auto-resolved
  EDI order, exc-047 OTHER (uncategorised → routed). All five classification
  chips (NEW_ORDER/ORDER_CHANGE/INQUIRY/COMPLAINT/OTHER) exercised. Click through
  `/cases` (EMAIL_ENTRY lens) to validate.
- ✓ **#6 DB persistence + scheduler** — `effect_outbox` table (V015, Postgres +
  SQLite) + tenant-scoped `OutboxRepository`; `orchestration/outbox` is now
  backend-pluggable (in-memory default, DB when DATABASE_URL set) so the queue
  survives restarts. Opt-in reconcile scheduler (`orchestration/outbox_scheduler`,
  wired into a FastAPI lifespan, gated by `ASOE_OUTBOX_RECONCILE_INTERVAL_S`,
  OFF by default). `tests/test_outbox_db.py` + `test_outbox_scheduler.py`.
- ✓ **#10 SSRF wired** — `gateways/attachment_fetch.AttachmentFetchGateway`
  validates every URL via the guard before fetch (stub blob in sandbox).

### Still PENDING (only what genuinely cannot be built now)
- **#10 SSRF** — inject a REAL fetcher into AttachmentFetchGateway (+ resolve=True)
  once a production attachment store exists (guard + stub are in place).
- **Constraint Graph** — deliberately NOT built: ADR §2.1/§5b say reuse
  `get_pipeline_topology` + `/exceptions/{id}/trace` and do NOT add a new
  surface; the Phase-6 Change Analysis section already renders the constraint
  data. (Not a buildable task — an architectural deferral.)
- **ADR-042 → Accepted** — compliance-gated on autonomy-v2 dual-control sign-off;
  must not be flipped unilaterally.

### Gates status — DoR safety gates (strategy §6): ALL IMPLEMENTED
- ✓ `tests/test_inbox_gate_openapi_contract.py` — **GREEN** (Phase 7): all 8
  section schemas exported; xfail marker removed → standing hard gate.
- **sanitizer**, **autonomy**, #2 (no auto-execute), #3 (SAP re-price cosign),
  **#4** (calibration), **#5** (send idempotency; correlation_id), **#6** (outbox
  + compensation queue), **#7** (SLO histogram), **#8** (gateway circuit breaker),
  **#9** (disposition hash chain), **#10** (XSS/CSP; SSRF deferred — no fetch
  path), **#11** (automation-bias: override-rate + Layer-2/dwell), **S** (sandbox
  isolation). Residue: #10-SSRF + #6-reconciler-worker (need a real fetch path /
  a background worker — infra, not gate logic).

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
> `docs/test-strategy/customer-inbox-tdd-strategy.md`. **Phases 0–8 are done** —
> the full Customer-Inbox feature port (MLS + Draft Reply + live WS + EDI 850 +
> Change Analysis + Knowledge Graph) plus the Phase-8 hardening **tractable
> subset** (contract snapshot, #4 calibration, #7 SLO histogram, #9 disposition
> hash-chain lock, axe sweeps). What remains is the **productionization
> backlog**, NOT feature work: the deep-infra DoR gates **#5** (correlation_id +
> delivery idempotency), **#6** (outbox/compensation), **#8** (email/SAP
> circuit-breaker parity), **#10** (XSS/CSP + SSRF allowlist), and **#11**
> Layer-2-open/dwell telemetry — each needs real instrumentation in the
> gateway/delivery/render core. ADR-042 stays *Proposed* until autonomy-v2
> dual-control sign-off (do NOT flip it unilaterally). Pick one gate, design it,
> land it test-first. Pre-prod: compliance sign-off waived, keep in-code
> Shadow/audit intact. Discipline: TDD, run tsc/tests before push, wait for CI
> green (10-min fallback) between tasks, audit against the plan before declaring
> complete.
