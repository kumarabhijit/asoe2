# ADR-031: Read-Projection Split-Trigger Conditions for Duplicate-PO

**Status:** Accepted (revisions per 2026-05-10 review)
**Date:** 2026-05-03 (initial); 2026-05-10 (revisions)
**Deciders:** Same as ADR-028 (review session 2026-05-03; sign-off review 2026-05-10).
**Applies to:** `db/migrations/` (when triggered), `db/repository.py`, `api/analysis_composer.py`, `observability/metrics.py`, `tests/perf/test_duplicate_po_query_latency.py` (new perf gate).
**Related:** ADR-028 (storage shape), ADR-032 (calibration deferral).

---

## Context

ADR-028 commits to the unified ASOE exception lifecycle for duplicate-PO V1 storage. The expert review (Kleppmann lens, ML/feature-store lens) accepted that decision conditional on a **pre-committed exit ramp** — an explicit, written set of conditions under which we will revisit and split out a per-intent read projection (materialized view, escalated to physical table only if MV is insufficient).

The point of this ADR is to make those conditions:

- **Quantified** — measurable from existing observability, not subjective.
- **Pre-committed** — written before V1 ships, so the decision to split (or not) is not relitigated under pressure later.
- **Bounded** — the action triggered is exactly "evaluate splitting `duplicate_check_results` as a read projection," not "rewrite the whole storage layer."

Without pre-commitment, two failure modes are likely:
1. The unified shape gets prematurely re-split on the first complaint, undoing the V1 simplification.
2. The unified shape is *never* split, even after it stops serving query patterns well, because nobody wants to litigate the storage decision again.

Both are common. The pre-committed trigger pattern is the standard mitigation (cf. SRE error-budget triggers, Conway's Law write-ups on pre-committed re-architecture decisions).

---

## Decision

Split `duplicate_check_results` into a dedicated **read projection** (materialized view first, escalated to a physical table only if MV is insufficient) when **any** of the following conditions hold for **two consecutive rolling weeks**:

> **2026-05-10 review revision (E5):** The two-week window is **rolling**, not calendar-aligned. Measurement uses a continuously-evaluated 14-day moving window.

### Trigger conditions (any one of these, sustained 2 rolling weeks)

| # | Condition | Measurement source | Threshold | Boundary |
|---|---|---|---|---|
| T1 | P95 latency on `GET /api/v1/exceptions/duplicates` (list endpoint) breaches budget | `observability/metrics.py` Prometheus histogram | **> 800 ms** | platform-wide |
| T2 | P95 latency on `GET /api/v1/exceptions/duplicates/:id` (envelope endpoint, ADR-028 Guard-rail 2) breaches steady-state warm-cache budget | Same | **> 200 ms** (warm) | platform-wide |
| T3 | Duplicate-PO query share of total exception-route DB time | pg_stat_statements + intent tagging | **> 30%** | **platform-wide** |
| T4 | Number of GIN indexes required on `ExecutionLog.recipe_output` to keep T1/T2 within budget | `db/migrations/` count + EXPLAIN ANALYZE plans | **> 2 indexes** | platform-wide |
| T5 | Calibration work is officially scheduled (proactive trigger from ML lens) | Roadmap / ticket marked "calibration kickoff" by **architecture chair** | **scheduled within next 90 days** | global |

> **2026-05-10 review revision (E2):** T3's measurement boundary is now **explicitly platform-wide** (moved from Open Questions to a binding part of the Decision). A noisy single tenant *should* trigger evaluation; that's the point. If multi-tenant skew becomes pathological, that's a separate ADR.

> **2026-05-10 review revision (E2 / ADR-028 alignment):** T2 threshold updated from `> 400 ms` to `> 200 ms` to match ADR-028's revised warm-cache latency budget. Cold-cache breach is a cache-design issue and is *not* a storage-shape trigger.

> **2026-05-10 review revision (E6):** T5 owner is the **architecture chair**. Any roadmap ticket tagged "calibration kickoff" must reference this ADR; the chair is responsible for filing the trigger evaluation when scheduling occurs.

The rolling two-week sustainment requirement filters transient spikes (a single bad week from a backfill or an outage doesn't trigger re-architecture).

T5 is *proactive* — it fires before calibration starts so the read projection is in place when training queries begin pulling labeled data, not after the calibration team is already blocked.

### Action triggered (binding scope)

Triggering this ADR causes one specific action: **evaluate splitting `duplicate_check_results` as a read projection.** The evaluation produces a follow-up ADR (e.g., ADR-040+) that decides:

- MV vs physical table
- Refresh strategy (sync trigger, async cron, logical replication)
- Index design for the projection
- Backfill plan for historical data

Triggering this ADR does **not** automatically perform the split. The follow-up ADR decides whether to proceed and how. This protects against false-positive triggers driving rework.

### Action explicitly NOT triggered

- Revisiting the unified shape for *other intents* (back-order, MOQ, etc.). Each intent has its own latency/share/cost profile; trigger evaluation is per-intent.
- Reverting to the spec's per-intent table layout. Even if T1–T5 fire, the answer is a *projection* (denormalized read model fed by the unified write model), not a parallel write model. ADR-028's CQRS posture is preserved.
- Schema redesign of `OrderEvent` or `ExecutionLog`. Those stay as-is; the projection sits beside them.

---

## Rationale

- **T1 / T2 (latency):** the UI's two hot paths. The user-experience SLOs in the spec (queue load, Layer-2 detail in one fetch) ride directly on these. T2 is the warm-cache steady-state budget; cold-cache transients are excluded from triggering.
- **T3 (query share):** even if absolute latency is fine, a single intent dominating DB time disproportionately means JSONB indexes are doing too much work and a typed projection would relieve them. Platform-wide measurement is intentional — a dominating tenant *should* surface.
- **T4 (index count):** Kleppmann's heuristic — once you're maintaining more than 2 GIN indexes on the same JSONB column, you've effectively hand-built a typed schema with worse ergonomics. Take the hint.
- **T5 (calibration scheduled):** ML lens's explicit condition for accepting unified V1. Pulling labeled training pairs out of nested JSONB across 10M rows is painful; doing it under deadline pressure is worse. T5 lets us land the projection *before* it becomes urgent. Architecture chair owns the trigger because they own the roadmap visibility.
- **Two-week rolling sustainment:** standard SRE practice for trigger-based decisions. Avoids reactive rework on transient spikes; rolling (not calendar-aligned) avoids "Monday-of-week-N didn't quite breach so the trigger never fires."

---

## Phased rollout

This ADR ships in V1 as **observability + alerting only**. The split itself happens only when triggered.

### V1 (observability scaffolding)

1. `observability/metrics.py` — Prometheus histograms tagged with intent for both query endpoints; pg_stat_statements integration tagged by intent.
2. `tests/perf/test_duplicate_po_query_latency.py` — synthetic perf gate run weekly in CI. Fails the build if T1 or T2 are breached *in CI's perf environment* (early warning, not the production trigger itself).
3. Weekly automated report: a small script that emits the current values of T1–T4 (rolling 14-day window) to a known dashboard / wiki page, so the trigger conditions are visible without anyone having to remember to look.
4. Roadmap discipline: T5 is owned by the architecture chair; any ticket tagged "calibration" must reference this ADR and trigger the evaluation.

### When triggered (future, no V1 work required)

1. Author follow-up ADR with MV/physical, refresh strategy, index design.
2. Implement projection.
3. Migrate read paths to use it (`api/analysis_composer.py` and `api/routes/exceptions.py`).
4. Backfill from `OrderEvent` + `ExecutionLog`.
5. Validate audit-chain integrity is preserved (the projection is read-only; it cannot diverge from the source of truth, but the test must prove it).

---

## Consequences

### Positive

- The unified-vs-split decision is bounded in time and triggered by data, not by debate.
- ML team has confidence the read projection will land before calibration work starts.
- The set of conditions is small and observable; nobody has to argue about whether to evaluate.
- Storage simplicity is preserved as long as it is genuinely simple to query against.

### Negative

- Adds a small ongoing observability cost (intent-tagged metrics, weekly report). Negligible.
- Risk of trigger gaming: someone optimizing one of T1–T4 in isolation to avoid the trigger. Mitigation: the triggers are independent, and the architecture chair has discretion to force evaluation if the *intent* of the trigger is being met (e.g., a heavy duplicate-PO query workload kept just under threshold by aggressive caching). Practical mitigation: review T1–T4 quarterly even when no individual condition is firing.
- Once triggered, the follow-up ADR + projection work is non-trivial (~2 sprints). Acceptable as long as it isn't a surprise.

### Compliance notes

- The projection (when it lands) is a read model — it is not the source of truth for SOX-relevant resolution state. Source remains `OrderEvent` + `ExecutionLog` + `audit_hash_chain`. The follow-up ADR must include a test proving the projection cannot diverge silently (refresh integrity check).

---

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **No pre-committed trigger; revisit ad hoc** | Recreates the "unified vs split" debate every quarter; either prematurely splits or never splits. Both are bad. |
| **Single-condition trigger (e.g., latency only)** | Misses the calibration use case (T5) and the "many GIN indexes" smell (T4). Latency alone is a lagging indicator; calibration is a leading one. |
| **Trigger fires immediate split, no follow-up ADR** | Removes the chance to evaluate whether the split is genuinely the right response to the trigger. False positives become rework. |
| **Tighter latency thresholds (e.g., T1 > 500ms)** | Premature optimization at V1 volume. The 800ms / 200ms numbers reflect what users will notice; tighter than that is engineering vanity. |
| **Calendar-aligned two-week window instead of rolling** | "Monday-of-week-N didn't quite breach so the trigger never fires" is a real failure mode. Rolling avoids it. |
| **Tenant-specific T3 measurement** | Misses the dominating-tenant signal that's exactly what the trigger is supposed to catch. Platform-wide is the right boundary. |

---

## Open questions

- Whether to add a T6 for **storage size** of `ExecutionLog.recipe_output` JSONB. If single rows balloon (e.g., very large signal_breakdown structures), JSONB performance degrades. Not added in V1; current data shape is small. Revisit if observability shows growth.
- Whether the trigger should apply to other intents that grow heavy query workloads. Each intent gets its own ADR if needed. This ADR is duplicate-PO-specific by design.

---

## References

- `docs/specs/duplicate-po/2026-05-03-design-review.md` (Item 6)
- `docs/specs/duplicate-po/2026-05-10-adr-review.md` (revisions)
- ADR-028 (Guard-rail 3 references this ADR)
- Kleppmann, *Designing Data-Intensive Applications*, Ch. 3 (storage / retrieval)
- SRE workbook patterns on error-budget triggers
