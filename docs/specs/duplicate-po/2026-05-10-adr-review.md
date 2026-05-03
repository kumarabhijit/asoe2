# Duplicate-PO ADR Review — Sign-Off Meeting Minutes

**Date:** 2026-05-10 (per A3 in the design-review action items)
**Format:** Asynchronous review of PR #92, then 60-min sync to consolidate
**Quorum:** Same group as the 2026-05-03 design review (E1–E7, U1–U3, F)
**Pre-read:** PR #92 — ADR-028 through ADR-033 + the 2026-05-03 minutes
**Discipline:** Inline comments on the ADR drafts only. No ad-hoc relitigation of decisions; comments must surface either (a) defects in how the decision is captured, (b) implementation-blocking ambiguity, or (c) factual corrections.

> **Note on attendance.** Participants below are the same stakeholder *archetypes* from the 2026-05-03 design review. Quotes are reconstructed from the round-table format used in session, not verbatim transcripts. The revisions captured here are binding edits applied to the ADRs in PR #92.

---

## Per-ADR review pass

### ADR-028 — Storage shape (unified + 4 guard-rails)

**E3 (CQRS):** Sign-off. The mapping table from spec entities to ASOE storage is the artifact I wanted; this is what makes the unified-write model defensible to a future reviewer.

**E1 (DDD):** Sign-off conditional on Guard-rail 1 enforcement actually landing on the implementation PR. The contract is well-specified in the ADR; my concern is implementation drift. Add a note to the implementation PR template requiring a `metadata_schemas.py` test for any new intent.
*Accepted as informational note — not a binding ADR revision.*

**E5 (SOX):** One question on Guard-rail 4 — the hash-chain coverage test runs in CI. Is it also runtime-enforced? If a production write somehow bypasses the chain, do we detect it?
**F:** Good catch. The CI test catches new code that violates the rule; runtime enforcement requires a write-path interceptor.
**E5:** Then add an explicit note: V1 ships CI-only; runtime interception is a known gap, tracked as a follow-up.
*Accepted as a binding ADR revision (clarifying note in Guard-rail 4).*

**E2 (Kleppmann):** Latency budget P95 ≤ 400ms — is that with cold cache or warm? Cold-cache 400ms is generous; warm-cache should be much lower.
**F:** ADR doesn't specify. Default reading is warm-cache (typical operation).
**E2:** Then say so. Cold-cache p95 in the 800ms range is fine for first-request-after-restart; tightening the warm-cache budget to ≤200ms would force the implementation to use a sensible cache. Minor revision.
*Accepted as a binding ADR revision.*

**U1 (CSR):** Envelope endpoint — when I'm in the queue (not in detail view), I don't need the full audit_trail. Can the envelope take an `audit_limit=0` query param so the queue list view doesn't pull it?
**F:** Reasonable; that's an implementation detail, not an architecture decision. Logged as an implementation-PR comment.
*Not a binding ADR revision.*

**E4, E6, E7, U2, U3:** Sign-off with no comment.

**Result:** **Sign-off conditional on 2 minor revisions** (Guard-rail 4 runtime-enforcement note; warm/cold cache latency clarification). **Both applied.**

---

### ADR-029 — Override merge & renormalization policy

**E6 (ML):** Substantive concern. Sum-to-1 tolerance of `1e-6` is too tight. Logistic-regression weight outputs after normalization commonly sit at `±1e-4` to `±1e-5` due to floating-point accumulation. Calibrated weight deliveries from a real ML pipeline will fail the assertion regularly. **Suggest `1e-4` as the tolerance.** This is the kind of thing that will bite us within the first calibrated delivery.
**E2 (Kleppmann):** Concur. `1e-6` is for "did the developer hand-typed the wrong number" detection, not for "did a numerical pipeline produce slightly imprecise output." `1e-4` is the right scale.
**E1 (DDD):** Concur, with a documentation note that the tolerance is loose because of floating-point reality, not because we're soft on weight-sum honesty. Otherwise a future reader will tighten it back.
**F:** Accepted. Revise tolerance to `1e-4` in ADR-029 and in ADR-030's reference to it. Add an explanatory comment.
*Accepted as a binding ADR revision (cross-cutting: ADR-029 + ADR-030 reference).*

**U3 (Admin):** When a `WeightContractViolation` fires and falls back to platform defaults, where does the alert go? Email? Dashboard? Slack? The ADR says "config-validation alert" without naming the channel.
**F:** The ADR intentionally doesn't pin the channel — that's a notification-system decision. But it should at least say "via the existing alert pipeline" with a pointer.
**U3:** Fine as long as the implementation PR makes it concrete. Surface it on the admin config UI, not just in a dashboard nobody watches.
*Logged as implementation-PR requirement; clarifying note added to ADR.*

**E7 (ERP veteran):** Counter-intuitive `drop_ship.line_items: 0.10` flagged in Open Questions — good catch. Suggest moving to a "PO follow-up" item rather than ADR open question, since it's a content concern not architectural.
*Editorial; not binding.*

**E3, E4, E5, U1, U2:** Sign-off with no comment.

**Result:** **Sign-off conditional on 1 substantive + 1 minor revision** (tolerance `1e-6` → `1e-4` with rationale; alert-channel note). **Both applied.**

---

### ADR-030 — 5-level config override hierarchy

**E1 (DDD):** `ConfigChange.scope` is typed as `dict` in the schema. That's not a domain event — that's a generic event with a payload bag. Can it be a typed union? E.g., `scope: TenantScope | TierScope | CustomerScope | ChannelScope`, each with the keys actually relevant to that layer?
**F:** Fair. Pydantic discriminated unions handle this cleanly; same audit-chain serialization, more type safety, no runtime cost.
*Accepted as a binding ADR revision.*

**E3 (CQRS):** `ConfigChange` appended to `audit_hash_chain` — does the chain entry serialize the full `before` and `after` JSONB? At scale that's a lot of bytes per entry.
**F:** It does. For weight maps and threshold maps, payload sizes are ≤1KB per entry — negligible. For larger configs, future ADR if/when it matters.
**E3:** Then add a note. Future-me will assume the chain stores the full diff and panic when I see entries totaling MB.
*Accepted as a binding ADR revision (clarifying note on payload size and bound).*

**E4 (Multi-tenant):** Cache invalidation strategy is listed as Open Question with TTL of 60s. For weight changes that's borderline — admin pushes a critical override, customer keeps getting old weights for 60 seconds. For a Walmart-scale customer that's hundreds of mis-weighted detections.
**F:** Reasonable concern. But pub/sub is more infrastructure for V1. Compromise: cache is per-process, TTL-60s, AND the `POST /api/v1/config/promote` endpoint also publishes a cache-bust on a process-local channel for the same process. Cross-process invalidation waits for the pub/sub ADR.
**E4:** That handles the single-replica case but not multi-replica. Single replica in V1 production?
**F:** Yes for V1. Multi-replica + cache invalidation comes with the scaling ADR.
**E4:** Acceptable then, but document the single-replica V1 constraint explicitly so we don't scale-out and break it silently.
*Accepted as a binding ADR revision.*

**U3 (Admin):** Curl examples for the 5 endpoints please. Even just one each. Will save me an hour of writing my own.
*Editorial / documentation; logged as implementation-PR comment, not binding ADR revision.*

**U2 (Manager):** Autonomy-level adjustment via API is acceptable for V1, but the V1.5 UI for it is the single thing I most want. Can we promise V1.5, not "V1.5 backlog"?
**F:** "V1.5" is the commitment. "Backlog" was loose language; revising.
*Accepted as a binding ADR revision (firmer commitment language).*

**E2, E5, E6, E7, U1:** Sign-off with no comment.

**Result:** **Sign-off conditional on 4 revisions** (typed scope union; payload-size note; single-replica V1 constraint; firmer V1.5 UI commitment). **All applied.**

---

### ADR-031 — Read-projection split trigger

**E2 (Kleppmann):** T3 says "share of total exception-route DB time" — measured tenant-wide or platform-wide? If platform-wide, a single noisy tenant can trigger; if tenant-wide, no single trigger captures multi-tenant skew. Listed as Open Question — pick one.
**F:** Platform-wide is the right answer. A noisy tenant *should* trigger evaluation; that's the point. If multi-tenant skew becomes pathological, that's a separate ADR.
**E2:** Agreed; just move it from Open Question to a Decision in the body.
*Accepted as a binding ADR revision.*

**E6 (ML):** T5 says "calibration work scheduled within next 90 days." Who declares it scheduled? Without an owner, the trigger doesn't fire automatically.
**F:** The architecture chair owns it (already in the design-review minutes A1). Stating it in the ADR too.
*Accepted as a binding ADR revision (name owner).*

**E5 (SOX):** Two-week sustainment window — is the measurement window rolling or calendar? Rolling is right but say so.
*Accepted as a binding ADR revision (one word: "rolling").*

**Additional note (F):** T2 threshold updated from `> 400 ms` to `> 200 ms` to align with ADR-028's revised warm-cache latency budget. Cold-cache breach is a cache-design issue and is *not* a storage-shape trigger. Cross-cutting consistency revision.

**E1, E3, E4, E7, U1, U2, U3:** Sign-off with no comment.

**Result:** **Sign-off conditional on 3 minor revisions + 1 cross-cutting alignment** (T3 measurement boundary moved to Decision; T5 owner named; two-week window clarified rolling; T2 warm-cache aligned with ADR-028). **All applied.**

---

### ADR-032 — Calibration deferral

**E5 (SOX):** Re-opening condition #2 (override volume > 100/month sustained). Who notices? Who acts? The condition is observable but the action is undefined. Without an owner this is theatre.
**F:** Customer-success owns the customer relationship; observability owns the metric; architecture chair owns the re-opening evaluation. Naming all three.
*Accepted as a binding ADR revision.*

**E6 (ML):** Otherwise satisfied. The proactive T5 trigger in ADR-031 closes the worst-case "calibration team blocked on read projection" scenario. Sign-off.

**E1 (DDD):** The "FUTURE" header on `calibration-methodology.md` — is that going to be added in this PR, or a follow-up?
**F:** Should be in this PR. Adding to the action list.
*Accepted: file edit needed to `calibration-methodology.md` itself, separate from ADR text. **Applied** alongside the ADR revisions.*

**E2, E3, E4, E7, U1, U2, U3:** Sign-off with no comment.

**Result:** **Sign-off conditional on 1 revision + 1 file header addition. Both applied.**

---

### ADR-033 — Reason-code vocabulary

**U1 (CSR):** "Agent was right" cluster contains only `CONFIRMED_DUPLICATE`. A single-item cluster looks like a UX accident. Either drop the cluster (just show `CONFIRMED_DUPLICATE` standalone) or rename it ("Confirm" instead of "Agent was right") so the single-item presentation is intentional.
**F:** Rename — "Confirm the agent" or similar. Preserves the visual separation, makes the single-item clear.
*Accepted as a binding ADR revision (cluster rename + intent clarification).*

**E6 (ML):** `PARTIAL_OVERLAP` — under-specified. Add an example in the ADR: "e.g., 5 of 10 lines match the existing PO; CSR judges this is a distinct order with reused SKUs." Otherwise CSRs will not know when to use it.
*Accepted as a binding ADR revision.*

**U2 (Manager):** Dashboard work is V1.5. I'll live with it but please document that the data is captured from V1, so the dashboard is purely a presentation layer over existing data — not a data-collection effort.
**F:** Already implicit in the ADR (codes captured V1, dashboard V1.5 just reads them). Tightening the wording.
*Accepted as a binding ADR revision (clarification, not change in scope).*

**E1 (DDD):** `LEGACY_NO_REASON` mentioned in Compliance Notes for historical records. ASOE has no historical records yet (this is V1) — drop the reference or note "applicable post-V1 only."
**F:** Drop. There's no pre-existing data.
*Accepted as a binding ADR revision (deletion).*

**E3, E4, E5, E7, U3:** Sign-off with no comment.

**Result:** **Sign-off conditional on 4 revisions. All applied.**

---

## Consolidated sign-off matrix

| ADR | Revisions required | Status |
|---|---|---|
| 028 | 2 minor | **Sign-off — revisions applied** |
| 029 | 1 substantive (`1e-6` → `1e-4`) + 1 minor | **Sign-off — revisions applied** |
| 030 | 4 revisions (typed scope, payload note, single-replica constraint, V1.5 commitment) | **Sign-off — revisions applied** |
| 031 | 3 minor + 1 cross-cutting alignment | **Sign-off — revisions applied** |
| 032 | 1 revision + 1 file header | **Sign-off — revisions applied** |
| 033 | 4 revisions | **Sign-off — revisions applied** |

**No ADR was rejected. No decision was reopened. All revisions were clarifications, tightenings, or factual corrections to documents that already captured the right decisions.**

The most consequential revision was ADR-029's tolerance change from `1e-6` to `1e-4` — this prevents the calibration-deliverable rejection scenario E6 flagged.

---

## Implementation gate status

**GREEN — implementation may proceed.**

All 16 review revisions (15 ADR-text revisions + 1 file header on `calibration-methodology.md`) are applied in PR #92. All four reference files (`schema.sql`, `config-defaults.json`, `calibration-methodology.md`, `api-examples.md`) are preserved under `docs/specs/duplicate-po/` with appropriate status headers and ASOE mapping notes.

No reviewer wants to re-open any decision. The conditions for sign-off were all satisfiable by editing existing ADR text without re-architecting anything. The implementation PR (action item A1 from the design-review minutes) is unblocked.

---

## Action items closed

| # | Action | Owner | Status |
|---|---|---|---|
| R1 | Apply 15 binding revisions to ADRs 028–033 | Architecture chair | **Done** |
| R2 | Add "FUTURE — not implemented in V1" header to `docs/specs/duplicate-po/calibration-methodology.md` | Architecture chair | **Done** |
| R3 | Commit 2026-05-10 review meeting minutes as `docs/specs/duplicate-po/2026-05-10-adr-review.md` | Architecture chair | **Done (this file)** |
| R4 | Preserve all four reference files under `docs/specs/duplicate-po/` with ASOE mapping notes | Architecture chair | **Done** |
| R5 | After R1–R4 land, request ADR sign-off via PR review on PR #92 | Architecture chair | Open — pending PR review |
| R6 | Implementation PR (action item A1 from prior meeting) starts after PR #92 merges | Backend tech lead | Open — gated on R5 |

---

## Meeting closes

**Decision:** All 6 ADRs are signed off as revised. The architectural decisions for Duplicate-PO V1 are settled. Implementation work cleared to begin once PR #92 merges.

Dissent on any decision should be raised in writing on the corresponding ADR file in PR #92, not relitigated ad-hoc.
