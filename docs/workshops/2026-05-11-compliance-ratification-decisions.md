# §28.1 Compliance Workshop — Binding Minutes

**Date:** 2026-05-11
**Mode:** Asynchronous — operator (PO + Compliance Veto Holder, offline-cleared) acted as chair against the pre-read at `2026-05-09-deferred-items-virtual-workshop.md`.
**Outcome:** All five §28.1 ratification gates **APPROVED AS-IS**. No conditions, no follow-up Compliance round required before §28.2 (operational soak + flip) can begin.

---

## Decisions

### 1A. ADR-038 §7.4 — Compaction protocol — **RATIFIED AS-IS**

The 8 per-event-type compaction templates in `knowledge/compaction/` plus the binding triggers (8 000 tokens / 25 events / 7 days) are approved without conditions. The `learning_signals:` frontmatter ML lens ask is already closed (commit `b7db08c`); no per-template Compliance checklist annex is required.

**Trade-off accepted:** Future template-format changes will require a fresh ratification round.

### 1B. ADR-038 §8.5 — CODEOWNERS map enforcement — **RATIFIED AS-IS**

The five-layer CODEOWNERS map shipped at `.github/CODEOWNERS` (both `asoe2` and `asoe-ui`) is locked. All placeholder team handles resolve to `@kumarabhijit @linkinrustle`; the per-layer comment structure stays so the mapping can re-fan-out if GitHub teams are created later.

**Trade-off accepted:** Bus factor of 2 on every audit-bearing path until org-level GitHub teams exist. No blocker for §28.2.

### 1C. ADR-039 §4.1 — Combination rule (asymmetric DOWNGRADE-only) — **RATIFIED AS-IS**

The bounded-blast-radius design holds: L2 LLM Shadow can ONLY apply `DISAGREE_DOWNGRADE` (GREEN → YELLOW). Upgrades (RED/YELLOW → GREEN) are forbidden. The `false_downgrade_rate ≤ 35%` X.3 ratification gate is wired (`shadow_llm_reviewer_override_rate_on_downgrades` gauge, Phase 28.6).

**Trade-off accepted:** Real L1 false-positives stay operator-resolved (no auto-correction by LLM). Symmetric variant (`DISAGREE_UPGRADE` for low-risk intents) explicitly rejected — Compliance keeps the asymmetric guarantee.

### 1D. ADR-039 §6 — Phased rollout X.1 → X.4 — **RATIFIED AS-IS (all four stages)**

The four-stage sequence is pre-ratified end-to-end:
* **X.1** (observe-only, all events) — already shipped
* **X.2** flip — `rollout.financial_impact_threshold_usd: 10000` after the 1-week soak
* **X.3** — threshold reduction post-soak, gated on `false_downgrade_rate ≤ 35%`
* **X.4** — cross-check extension on deterministic-primary

Each stage retains its rollback runbook (`docs/runbooks/shadow_llm_x2_rollback.md` §3.1.A — manual SIGHUP). Auto-rollback on out-of-band disagreement explicitly **deferred** — operator preserves the manual-rollback decision.

**Trade-off accepted:** Operator-driven rollback; no auto-rollback hook required this phase.

### 1E. ADR-040 §2 + §2.2 — Case-level cosign + SoD — **RATIFIED AS-IS**

Case-level four-eyes cosign with strict SoD (cosigner ≠ initiator) is approved. Code path behind `ASOE_CASE_COSIGN_ENABLED` (commit `6eb66ad`) is cleared for the §28.2 flip. No emergency-bypass key, no SoD waiver on single-record cases — both explicitly rejected to keep the SOX §404 control unweakened.

**Trade-off accepted:** Single-record cases carry the same two-reviewer overhead as multi-record ones; operability under single-shift incident conditions relies on the standard escalation path, not a bypass.

---

## What unblocks now

§28.2 (operational soak + flip) is no longer workshop-gated. The 1-week observe-only X.1 soak with the Azure provider can begin immediately. The downstream flips (X.2, case-cosign, case-agent routing, OCR) can sequence per the existing operational checklist:

```
Week 0 (now):     X.1 soak begins — scrape /api/v1/metrics for 7 days
Week 1:           Review SLI band targets; if disagreement_rate in 5–15%
                  and validation_errors near zero → X.2 flip
Week 1+:          ASOE_CASE_COSIGN_ENABLED=1 (independent of X.2)
Week 1+:          ASOE_CASE_AGENT_ENABLED=1 (independent of the above)
Week 1+:          ASOE_OCR_PRIMARY=azure_di + AZURE_DI_* env vars
Week 4 (target):  X.3 threshold reduction gated on false-downgrade ≤ 35%
Future:           X.4 cross-check expansion (separate ratification round)
```

§28.3 (ML follow-up on curated `(intent, reason_tag)` tuples) remains out-of-engineering-scope until the curated dataset matures.

---

*Minutes recorded 2026-05-11 by the operator (PO + Compliance Veto Holder, offline clearance). Pre-read at `2026-05-09-deferred-items-virtual-workshop.md` is the source for the per-lens positions and the trade-offs the operator weighed.*
