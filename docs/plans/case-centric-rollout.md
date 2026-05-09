# Case-Centric Architecture — Rollout Decisions and Execution Plan

**Document type:** Operational planning doc (not an ADR — references ADR-038 / ADR-039 for binding decisions)
**Status:** Active
**Date:** 2026-05-06
**Owners:** Principal AI/Agentic Engineering Architect; Engineering leads per phase
**Tracks:** ADR-038 (case-centric order intake); ADR-039 (LLM Compliance Shadow second opinion)

This document consolidates the four meta-decisions raised after ADR drafting:
1. Whether `architecture_v5.md` is needed (and when).
2. Whether `asoe-ui` architecture changes are needed.
3. The phase-wise implementation plan (consolidating ADR-038 §9 with operational sequencing).
4. Whether to incorporate the in-flight Phase A/B/G code (on `claude/review-order-entry-architecture-RCIUa`) into the rollout, and what adaptation is needed.

It also enumerates the testing strategy per phase.

---

## 1. Decision: `architecture_v5.md` — yes, but not yet

### 1.1 The architecture-versioning discipline (per `architecture_v4.md` §14)

`architecture_v4.md` itself defines when a new major version is warranted:
> *A new architecture version ships when:*
> *— A cross-cutting governance principle changes…*
> *— The graph topology changes structurally…*
> *— The persistence model changes…*
> *— Three or more ADRs accumulate that touch the same surface area.*

ADR-038 + ADR-039 between them trigger **all four** conditions:
* **Governance:** the L0 Knowledge Layer is a new first-class tier with its own CODEOWNERS / lifecycle / audit story.
* **Graph topology:** the Case Agent (L3) sits above the existing graph; the harness (L4) extends `shadow_audit` for the L2 LLM second opinion. Tier-2/3 paths flow through new orchestration nodes.
* **Persistence:** `OrderCase` parent entity + `case_correlation_keys` table + per-case event log + compaction-summary persistence.
* **Cumulative ADRs:** ADR-034 §6 (in-flight) + ADR-038 + ADR-039 (and the eventual ADR-035 / ADR-036 / ADR-037 / ADR-040 that are referenced) all touch the same surface area.

**`architecture_v5.md` IS warranted.**

### 1.2 But not now — `architecture_v4` discipline says "what is done"

`architecture_v4.md`'s opening note reads:
> *v4 is the stable re-baseline of what is **done** as of 2026-05-01.*
> *ADR-026 / ADR-027 are Proposed and not absorbed; their architectural content will be absorbed into v4.1 once they ship + ratified.*

Per that discipline, `architecture_v5.md` should be drafted **when ADR-038/039 are ratified AND Phase H.1 has shipped + been validated.** Drafting it now repeats the v3-failure-mode of "writing aspirations into the spec."

### 1.3 What we do now

* **Add a one-paragraph addendum to `architecture_v4.md`** noting that ADR-038 + ADR-039 are Proposed and will trigger `architecture_v5.md` once ratified and the first phase ships. Lineage hint for any reader landing on v4.
* **Defer the actual `architecture_v5.md` draft** until Phase H.1 ships (≈Phase H.4 milestone is the natural cut-over point — by then, L0 is real, OrderCase is persisted, the agent doesn't yet run, and v5 documents what's done).

This matches v4's own versioning discipline applied recursively. No new file in this commit; a small addendum is added to v4.

---

## 2. Decision: `asoe-ui` architecture changes — direction noted now, code lands in Phase H.6

### 2.1 What changes for the UI

ADR-038 §9 (Phase H.6) commits to:
* `/cases` as the primary CSR surface (a new top-level page).
* `CaseDetailPanel` reshape of the existing `ExceptionDetailPanel`. Existing `*Section.tsx` components mount on the case detail via the existing data-presence pattern (no per-intent dispatch — Guardrail #1 preserved).
* `/inbox` and `/exceptions` retain as **filtered case-list views** for ADR-034 §6 backward compatibility, but the primary CSR surface is `/cases`.

This is meaningful UI work, comparable in scope to the section-component work done in ADR-034 Phase G.

### 2.2 What we do now (and what we defer)

* **Add a notice section** to `asoe-ui/ui_architecture.md` explaining the upcoming case-centric direction, pointing at ADR-038 §9 / Phase H.6 for the implementation phase. Frontend Platform team has visibility ahead of when the work lands.
* **No UI code changes in this branch.** UI work lands in Phase H.6 (≈ weeks 7–9 of the rollout). Design discussion can start immediately against the case data model from Phase H.2.
* **Existing UI tests + lock tests stay green.** ADR-038 explicitly preserves Phase G's UI work; nothing breaks.

### 2.3 What needs design before Phase H.6 starts

* `/cases` list view: column choice, default sort (SLA-deadline driven), filter chips.
* `CaseDetailPanel` layout: case header + child-record stack + action buttons.
* SLA visual treatment: at-risk indicators, deadline countdowns, breached-state surfacing.
* Migration path: how `/inbox` and `/exceptions` become filtered views without rebuilding their existing UX.

These are Frontend Platform + UX deliverables, owned by their team. This document flags the dependency; design specifications are their authoring track.

---

## 3. Decision: incorporating the in-flight Phase A/B/G code (`claude/review-order-entry-architecture-RCIUa`)

### 3.1 What's on that branch

The `claude/review-order-entry-architecture-RCIUa` branch carries:

| Component | Location | Status |
|---|---|---|
| `EMAIL_ORDER_ENTRY` Intent + recipe + skill | `recipes/EmailOrderEntryRecipe.py`, `skills/email-order-entry_SKILL.md` | Phase A — committed, tested |
| Vocabulary sync (specs.py, models.py, fallback_backend) | `constraints/specs.py`, `contracts/models.py`, `constraints/fallback_backend.py` | Phase A — committed |
| `email_intake` gateway + 5 ops | `gateways/`, `tests/conftest.py`, `api/sandbox_gateways.py` | Phase B — committed, tested |
| `EmailOrderEntryAnalysisData` + adapter + audit-bearing registry | `api/schemas.py`, `api/analysis_adapters.py`, `compliance/audit_bearing_registry.yaml` | Phase B — committed, tested |
| `EmailSourceData` + adapter + section + back-link | `api/`, `asoe-ui/src/`, mock exc-026 | Phase G — committed, tested |
| 18+ component tests + architectural lock tests | `tests/components/`, `tests/architectural/` | Phase C / G — committed |

### 3.2 The interaction with Phase H.1 (knowledge layer migration)

Phase H.1 of ADR-038 migrates **every** existing `skills/<name>_SKILL.md` to `knowledge/skills/<name>/SKILL.md` + `metadata.yaml`. The `email-order-entry` SKILL.md sits in that "every" set — but it only exists on the in-flight branch.

Two options:

**Option A: Land the in-flight branch first; case_centric_architecture rebases on top.**
* Order: merge `claude/review-...` → main → rebase `case_centric_architecture` onto main → Phase H.1 picks up email-order-entry naturally.
* Pros: clean dependency chain; no duplicate work; Phase H.1 covers all skills uniformly.
* Cons: gates `case_centric_architecture` on the review/merge of the in-flight branch.

**Option B: Cherry-pick the in-flight Phase A/B/G commits into case_centric_architecture.**
* Order: cherry-pick A→B→G onto case_centric_architecture; then Phase H.1 covers all 10 skills.
* Pros: case_centric_architecture is independently reviewable; no external gate.
* Cons: duplicates commits; the eventual merge of `claude/review-...` to main will conflict with `case_centric_architecture`'s history; merge-resolution overhead.

**Option C: Proceed with case_centric_architecture against current main (9 skills).**
* Order: Phase H.1 migrates the 9 skills currently on main; email-order-entry skill is migrated to bundle structure when `claude/review-...` lands. The bundle-migration becomes part of that branch's eventual rebase / squash-merge.
* Pros: no cherry-pick complexity; both branches stay independently reviewable; main grows incrementally.
* Cons: when `claude/review-...` rebases / merges, its `email-order-entry` work needs minor adaptation (move the SKILL.md into bundle structure; create metadata.yaml); ~30 min of work at merge time.

### 3.3 Recommendation

**Option C — proceed with case_centric_architecture against current main.**

Rationale:
* Cherry-picking creates a duplicate-commit headache that resolves messily at merge time. Option B's cleanup cost > Option C's adaptation cost.
* Option A creates a hard dependency between two branches that are otherwise independent. Slows down `case_centric_architecture` review for no architectural benefit.
* Option C keeps the two efforts decoupled. When `claude/review-...` is ready to merge, it carries a small "migrate to knowledge/skills/email-order-entry/" commit as part of the rebase. The migration is mechanical (the same 4 files Phase H.1 produces for every other skill).

### 3.4 The in-flight branch's adaptation requirement — RESOLVED in this branch

When the in-flight Phase A/B/G work was rebased on top of the case-centric stack (forming `claude/review-order-entry-architecture-RCIUa`), the bundle-migration was carried out as a coherence-fix commit on the same branch:

1. ✅ `git mv skills/email-order-entry_SKILL.md knowledge/skills/email-order-entry/SKILL.md` (history preserved).
2. ✅ `knowledge/skills/email-order-entry/metadata.yaml` authored matching the pattern Phase H.1 established for the other 9 skills.
3. ✅ No tests reference the old path (`skills/loader.py::select_for_event` uses the legacy filename string as the lookup key, which `_bundle_name_from_legacy_filename` resolves to the bundle path transparently).
4. ✅ Empty `examples/`, `assets/`, `specs/` directories created per ADR-038 §5.5 (examples are *earned* by real failures, not authored speculatively).
5. ✅ Spec relocated: `docs/specs/order-entry-from-email-product-spec.md` → `knowledge/skills/email-order-entry/specs/order_entry_spec.md`. The 4 reference paths (recipes/EmailOrderEntryRecipe.py, contracts/policy.py, docs/adr/ADR-034-email-order-entry-skill.md, this plan) updated in the same commit.

After this commit, **all 10 skills** live under `knowledge/skills/<name>/`. The H.1 invariant ("every skill is a bundle") holds.

---

## 4. Consolidated Implementation Plan (Phase H.1 → H.7 with operational detail)

This is ADR-038 §9 with operational sequencing, owner mapping, and concrete acceptance criteria per phase.

### Phase H.1 — Knowledge layer foundation (1 week, this branch)

**Owner:** Engineering (this session).
**Acceptance:**
* `knowledge/skills/<name>/` exists for each existing skill with `SKILL.md`, `metadata.yaml`, empty `examples/` / `assets/` / `specs/` directories.
* `metadata.yaml` is parseable; CI fitness test verifies schema.
* `skills/loader.py` updated to read from `knowledge/skills/<name>/SKILL.md` with backward-compat fallback to `skills/<name>_SKILL.md` for one release.
* Existing pytest suite green.
* New `tests/test_knowledge_bundle.py` covers the bundle structure invariants.

### Phase H.2 — `OrderCase` primitive + correlation table (1 week)

**Owner:** Backend Engineering.
**Acceptance:**
* `contracts/models.py` += `OrderCase` Pydantic model.
* `db/migrations/V009__order_case.sql` + `V010__case_correlation_keys.sql`.
* `api/store.py` + `db/repository.py` += `OrderCase` CRUD + correlation lookup-or-create.
* `ExceptionRecord.parent_case_id` added (nullable).
* No behavioural change in existing flows.
* New tests: `tests/test_order_case.py` for CRUD + correlation lookup.

### Phase H.3 — Tier-2 case materialisation on existing flows (2 weeks)

**Owner:** Backend Engineering + Compliance review.
**Acceptance:**
* `orchestration/nodes.py::build_analysis` opens an `OrderCase` lazily on non-clean events; sets `parent_case_id` on the record.
* SLA clock starts on case open; `OrderCase.sla_deadline` populated from a stub policy table.
* Existing e2e tests verify `parent_case_id` is set on non-clean records and a case row exists.
* Optional backfill job (deferrable).

### Phase H.4 — L2 attachment-extractor primitive (2 weeks, parallel with H.5)

**Owner:** Backend Engineering + Tools Admin (vendor pick).
**Acceptance:**
* `agents/primitives/extract_attachment.py` ships with format dispatch.
* Template-fingerprint cache (per-tenant; per ADR-038 §5.8).
* CI fixtures: ≥5 representative document shapes per format.
* Cost + latency telemetry meet ADR-038 §8.2 budget.

### Phase H.5 — Case Agent (L3) + Harness extensions (L4) (3-4 weeks, **load-bearing phase**)

**Owner:** Senior Backend Engineering + Compliance review.
**Acceptance:**
* `agents/case_agent.py` — the loop with the 18-tool surface (ADR-038 §6.4).
* `agents/working_memory.py` — Karpathy cache-discipline order honoured (ADR-038 §5.3).
* `agents/budget.py` — per-tier budget enforcement.
* L4 harness extensions: case-aware concurrency lock; tool-call interception; tier graduation.
* Initially routes only NEW Manual Order events through the agent (none exist in production until ADR-034 ships).
* Mocked-LLM unit tests + e2e with stub LLM responses producing each terminal state.

### Phase H.6 — UI: case detail surface (2 weeks)

**Owner:** Frontend Platform + UX.
**Acceptance:**
* `/cases` list view with SLA-driven sort, filter chips.
* `CaseDetailPanel` reshape; existing `*Section.tsx` mount via data-presence.
* `/inbox` + `/exceptions` retain as filtered views.
* Existing UI tests + lock tests stay green; new architectural lock test for `/cases` page.

### Phase H.7 — T3 compaction + SLA tracking + backfill (2 weeks)

**Owner:** Backend Engineering + Compliance ratification.
**Acceptance:**
* Compaction trigger fires; templates active in `knowledge/compaction/<event_type>.template.md`.
* `tests/test_compaction.py` verifies replay-divergence == 0.
* SLA per customer-tier policy table at `knowledge/policy/sla_per_customer_tier.yaml`.
* Backfill job materialises orphan cases for legacy `ExceptionRecord` rows.
* Migration of override / cosign / disposition flows to operate on case lifecycle.

### Total timeline

10–12 weeks of focused engineering. T1 traffic uninterrupted throughout. **Phase H.5 is the risk concentration** — recommend spike against `email-order-entry` first (a single skill) before generalising.

---

## 5. Testing Strategy Per Phase

Each phase has its own test surface. The discipline is **"every phase ships green tests; no phase merges without."**

### 5.1 Phase H.1 — Knowledge bundle tests

* `tests/test_knowledge_bundle.py`:
  * Every bundle in `knowledge/skills/` has `SKILL.md`, `metadata.yaml`.
  * `metadata.yaml` parses to the schema.
  * `runtime_includes` allowlist is honoured by the loader (request outside the allowlist hard-fails).
  * `bundle_version` SemVer-valid.
  * `anchor_examples` ≤ 2.
  * `cached_prefix_max_tokens` ≤ 3000.
* `tests/sandbox/test_recipe_integrity.py` — updated paths; existing recipe-loadability tests stay green.
* `tests/test_skills_loader.py` (existing) — back-compat fallback to old path tested explicitly.
* Existing pytest suite (full) — no regressions.

### 5.2 Phase H.2 — OrderCase tests

* `tests/test_order_case.py`:
  * Pydantic model validation (forbid extra; required fields; literal constraints).
  * CRUD round-trip.
  * Correlation lookup-or-create (single key, multi-key, missing key).
  * Multi-PO email opens N cases.
  * Email matching pre-existing Automated case attaches (no new case opened; source_channel becomes a list).
* `tests/test_db_migrations.py` — V009 / V010 forward + idempotent + rollback.
* Full pytest no-regression.

### 5.3 Phase H.3 — Lazy materialisation tests

* `tests/test_e2e_case_materialisation.py`:
  * Clean Automated event → no case opened; `parent_case_id = None`.
  * Non-clean Automated event → case opened; `parent_case_id` set; SLA stamped.
  * Manual Order event → case opened eagerly; SLA stamped at email-receive timestamp.
* All existing e2e tests parameterise on `parent_case_id is None` for T1 path; add coverage for T2 path.

### 5.4 Phase H.4 — Extractor tests

* `tests/test_extract_attachment.py`:
  * Native PDF: text extraction → structured fields.
  * Scanned PDF: OCR + LLM → structured fields.
  * Excel: openpyxl + LLM → structured fields.
  * Image: multimodal LLM → structured fields.
  * Cache hit: same template fingerprint → no re-extraction.
  * Tenant isolation: same fingerprint, different tenant → cache miss (validates §5.8).
* Cost telemetry: per-format expected cost band asserted.

### 5.5 Phase H.5 — Case Agent tests

* `tests/test_case_agent.py`:
  * Mocked LLM returning each terminal action — agent loop reaches each terminal state.
  * Tool calls executed in order; persisted to episodic memory.
  * Budget exhaustion → escalate.
  * Tool failure as observation → agent reasons about it.
  * Replay: same case state + same mocked LLM responses → same tool trace.
* `tests/test_working_memory.py`:
  * Cache-discipline order asserted (system → SKILL.md → anchor examples → manifest summaries → per-turn).
  * Order regression fails build.
* `tests/test_e2e_case_agent.py`:
  * Manual Order full flow: email arrives → case opens → agent runs → terminal state.
  * Multi-event case: events 1, 2, 3 all attach to same case via correlation; agent reads prior history each time.

### 5.6 Phase H.6 — UI tests (asoe-ui)

* `tests/components/CaseDetailPanel.test.tsx`:
  * Renders case header (source, channel, SLA).
  * Renders child sections via data-presence (no intent-keyed dispatch).
* `tests/architectural/case_detail_data_presence.test.ts`:
  * No `intent === ...` runtime dispatch in `/cases/`.
  * `*Section` mounts unchanged.
* `tests/test_cases_page.tsx`: list view, SLA sort, filter chips.
* Existing UI tests (642 from Phase G) stay green.

### 5.7 Phase H.7 — Compaction / SLA / backfill tests

* `tests/test_compaction.py`:
  * Trigger fires at 8k working-memory / 25 events / 7 days.
  * Compaction template applies deterministically.
  * Replay-divergence == 0% (compacted view + underlying events agree).
  * Compaction events themselves audit-logged.
* `tests/test_sla.py`:
  * Per-customer-tier deadline computed correctly.
  * SLA breach event fires.
* `tests/test_backfill.py`:
  * Legacy orphan `ExceptionRecord` rows get auto-generated parent cases.
  * Optional merge pass groups records by `(tenant, customer, customer_po)`.

### 5.8 Cross-phase test discipline

* **Standalone tests** (recipe purity, no orchestration): unchanged shape; recipes stay pure.
* **Sandbox tests** (`tests/sandbox/`): expanded to cover bundle-integrity + case-integrity invariants.
* **Unit tests**: per-phase (above).
* **E2E tests**: every phase adds at least one happy-path + one failure-mode e2e.
* **Architectural lock tests**: data-presence dispatch at `/cases`; bundle-runtime allowlist; combiner asymmetry (ADR-039); tenant-cache-key isolation.

---

## 6. Open execution risks (honest)

1. **Phase H.5 is genuinely hard.** The Case Agent's tool surface, working-memory loader, and budget enforcer involve real engineering. The estimate of 3-4 weeks could slip; spike against email-order-entry first.
2. **Phase H.6 UI work depends on H.3.** Frontend can start design but cannot ship until H.3 lands `parent_case_id`.
3. **Multimodal extractor (H.4) cost has high variance.** Real customer documents vary widely; the $0.045 per-event amortised target depends on cache discipline that won't be measurable until production traffic flows.
4. **Compliance ratification cadence.** §7.4 (compaction) and §8.5 (governance) of ADR-038 + §4.1 + §6 of ADR-039 each need workshop ratification. Compliance workshop slots are ~1/month; sequencing matters.
5. **The in-flight branch's eventual rebase** — Option C (§3.3) requires a small adaptation when `claude/review-order-entry-architecture-RCIUa` merges. Documented in §3.4.

---

*This document tracks the ADR-038 / ADR-039 rollout. It is operational, not architectural. Updates land here as phases complete; the architecture story stays in the ADRs and the future `architecture_v5.md`.*
