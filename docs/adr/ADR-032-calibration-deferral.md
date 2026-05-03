# ADR-032: Calibration Deferral and Future-State Contract

**Status:** Accepted
**Date:** 2026-05-03
**Deciders:** Same as ADR-028 (review session 2026-05-03). ML/feature-store lead with veto on the deferral; concurrence given conditional on ADR-031 T5 trigger and ADR-030 5-level hierarchy landing in V1.
**Applies to:** No code changes in V1. Architectural commitment that shapes ADR-029, ADR-030, ADR-031, ADR-033 and the future calibration service.
**Related:** ADR-028, ADR-029, ADR-030, ADR-031, ADR-033.

---

## Context

The new `b2b-duplicate-po-check` skill specification ships with a substantial reference document — `docs/specs/duplicate-po/calibration-methodology.md` — describing a 3-phase weight calibration program:

- **Phase 1 — Baseline Calibration (pre-launch, 2–4 weeks):** logistic-regression fit against labeled history, threshold optimization on precision/recall, hold-out validation.
- **Phase 2 — Supervised Learning (days 1–60 live):** L1/L2 autonomy only; mandatory override reason codes feed weekly calibration reports; weights adjust incrementally.
- **Phase 3 — Continuous Tuning (ongoing):** monthly batch re-fit, drift detection, autonomy graduation/downgrade.

Per `prompts/po-spec-to-asoe.md` HALT condition #5 ("the spec describes a feedback / retraining loop — that is out of scope for the deterministic recipe layer"), the calibration loop is not directly implementable inside the Skill–Shadow–Recipe model.

The product-owner direction received during the design conversation:

> "in beginning we are not planning to have any calibration, but expect it to be provided by PO/customers, its more of a futuristic requirement"

This is an explicit architectural decision, not a deferral by neglect. It needs to be recorded so future contributors don't:

1. Try to implement calibration inside the recipe layer (which would violate the deterministic-recipe contract).
2. Forget that the system *expects* calibrated values to arrive from outside, and architect away the surface that receives them.
3. Treat the calibration reference document as authoritative spec for V1 work.

The deferral has consequences for adjacent decisions — specifically, it elevates the 5-level config override hierarchy (ADR-030) from "nice to have" to "must have in V1," because that hierarchy is the channel through which customer-supplied calibrated values enter the system.

---

## Decision

### A. Scope of deferral

**No calibration loop is built in V1.** Specifically not built:

- No logistic-regression fitting pipeline.
- No labeled-history extraction harness.
- No weekly calibration report generator.
- No drift-detection alerting.
- No autonomy graduation/downgrade controller.
- No automated weight re-fit batch job.

### B. What V1 *does* deliver to make calibration possible later

V1 ships the architectural surface that future calibration work will plug into. Without these, calibration is impossible regardless of effort spent on the loop itself:

1. **Inbound channel for customer-supplied calibrated values** — the 5-level config override hierarchy from ADR-030. Customers deliver calibrated weight maps + thresholds + per-tier windows via `POST /api/v1/config/:intent/:layer`. Validated by ADR-029's merge + sum-to-1 contract; audited by ADR-030's `ConfigChange` event.
2. **Structured override reason codes** — the 8-code vocabulary from ADR-033 (`INTENTIONAL_REORDER`, `AMENDED_PO`, `BLANKET_RELEASE`, `SYSTEM_RETRY_VALID`, `DIFFERENT_SHIP_TO`, `CONFIRMED_DUPLICATE`, `PARTIAL_OVERLAP`, `OTHER`). Every human override is captured with a reason code, producing the labeled training data calibration will eventually need.
3. **Per-event signal breakdown in the audit trail** — `ExecutionLog.recipe_output.signal_breakdown` (ADR-028 Guard-rail 1) records the 8 weighted contributions for every detection, available alongside the human resolution. The `(signal_breakdown, recommended_action, resolved_action, override_reason, customer_id, channel, timestamp)` tuple is the calibration training row format.
4. **Read-projection split trigger T5** — ADR-031 includes "calibration work scheduled within next 90 days" as a *proactive* trigger. When calibration eventually starts, the read projection lands first, so training queries don't fight JSONB extraction.

### C. Future-state contract

When calibration is greenlit (V2 or later), it will be a **separate service**, not an in-process recipe extension. Binding shape:

```
[duplicate-PO ExecutionLog rows]   →   [training data extractor]
                                              │
                                              ▼
                                    [calibration service]
                                              │
                                              ▼
                                    [proposes new weights]
                                              │
                                              ▼
                            [admin reviews + promotes via ADR-030 API]
                                              │
                                              ▼
                          [tenant_config rows updated for the customer]
                                              │
                                              ▼
                          [DuplicatePORecipe.py reads new weights via gateway]
```

Recipes never call calibration directly. Calibration never writes to recipes. The only coupling point is `tenant_config`, mediated by admin review. This preserves:

- **Deterministic recipe layer** — recipes remain pure, auditable, and reproducible.
- **Audit-chain integrity** — calibrated weights enter the system through the same `ConfigChange` event path as manually-edited weights. No back door.
- **Human-in-the-loop control** — admin reviews proposed weights before promotion; auto-promotion is a separate decision deferred to a future ADR.

### D. Disposition of the calibration reference document

`docs/specs/duplicate-po/calibration-methodology.md` is **preserved verbatim** as a forward-looking reference, with an explicit header noting:

> **Status:** FUTURE — not implemented in V1.
> Per ADR-032, calibration is deferred. This document describes the eventual calibration program; customer-supplied calibrated values enter the system today via the ADR-030 config override hierarchy. Do not implement any portion of this document in V1.

This serves three purposes:
- Future contributors find the design without re-deriving it.
- The PO's reference content is preserved (no information loss).
- The "FUTURE" header prevents accidental V1 implementation.

### E. Explicit re-opening conditions

This ADR is re-opened (i.e., calibration moves from deferred to scheduled) when any of:

1. A customer contractually requires automated calibration as part of a deal.
2. Operational override volume per customer exceeds 100/month sustained, indicating the manually-supplied calibration is failing to keep up with drift.
3. Multiple customers (≥3) request calibration within a quarter.
4. Architecture chair determines that the deferred work has accumulated sufficient debt that the surface needs to be built anyway.

Re-opening produces a calibration ADR (ADR-040+) that formally scopes Phase 1 / Phase 2 / Phase 3 implementation, including ADR-031 T5 firing to land the read projection first.

---

## Rationale

- **Honors the recipe-layer contract.** `prompts/po-spec-to-asoe.md` HALT #5 is binding for a reason — recipes are deterministic, auditable, and reproducible. A feedback loop inside the recipe layer would compromise all three. Calibration belongs outside the recipe layer.
- **Honors the product-owner direction.** Explicitly recorded so future contributors don't second-guess.
- **Doesn't paint into a corner.** ADR-030 + ADR-033 + ADR-031 collectively ensure the data and surfaces calibration will need are in place. The deferral is a delay, not an architectural mistake.
- **Bounded re-opening.** Conditions in §E are concrete. The deferral is not "indefinite"; it is "until one of these things happens."
- **Document discipline.** Keeping the calibration spec as a "FUTURE" reference avoids the worse failure mode of *deleting* the reference (information loss) or *implementing it partially* (drift between what's coded and what's specified).

---

## Phased rollout

V1 ships **no calibration code** and **no calibration infrastructure**. The phased rollout for this ADR is purely about the supporting surfaces in adjacent ADRs:

### V1 (no work in this ADR; cross-references)

- ADR-030: 5-level override hierarchy lands. Calibrated values have an inbound channel.
- ADR-029: weight contract validates calibrated submissions.
- ADR-033: structured override reason codes capture training data.
- ADR-031 T5: read-projection trigger pre-arranged for when calibration starts.

### When re-opened (future ADR-040+)

1. Calibration ADR scoping Phase 1 / Phase 2 / Phase 3.
2. Read projection lands per ADR-031 T5.
3. Calibration service deployed as a separate workload.
4. Admin review + promote workflow lands in `asoe-ui` (likely V1.5 already covers the manual-edit version of this UI).
5. Telemetry: detection rate, false-positive rate, override-by-reason distribution become first-class dashboards.

---

## Consequences

### Positive

- V1 ships without the complexity of an ML loop.
- The deferral is intentional and recorded — no ambiguity for future contributors.
- Customers can supply calibrated weights manually from V1 day one.
- Adjacent decisions (ADR-029, ADR-030, ADR-031, ADR-033) are all aligned to calibration's eventual needs.
- The recipe layer's deterministic contract is preserved.

### Negative

- Customers without internal calibration capability will run on platform/tier defaults, which the calibration spec explicitly calls "starting-point heuristics, not production weights." Mitigation: tier presets in `config-defaults.json` give a reasonable baseline; per-customer drift surfaces as elevated override rates, which manager-facing dashboards (per ADR-030 V1.5) can highlight.
- Override-reason data is captured but not actively exploited until calibration starts. Acceptable cost; the data is small, structured, and auditable.
- The "FUTURE" reference document is at risk of going stale relative to the eventual implementation. Mitigation: when calibration ADR (ADR-040+) is written, the reference document is updated to match the actual scope at that point and the FUTURE header is removed.

### Compliance notes

- Manually-supplied calibrated weights enter through the same `ConfigChange` audit path as any other config edit. SOX-traceable from V1.
- When automated calibration eventually lands, the same `ConfigChange` audit applies — calibration-proposed weights are written by a service identity (`actor_role: "CALIBRATION_SERVICE"`), reviewed and promoted by a human admin, with both events captured.

---

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Implement calibration inside the recipe** | Violates `prompts/po-spec-to-asoe.md` HALT #5 (recipes must be deterministic, no feedback loops). Compromises audit reproducibility. |
| **Implement Phase 1 only (pre-launch baseline calibration)** | Phase 1 alone is a one-shot data-science exercise, not infrastructure; valuable but not deferred-vs-built — when a customer supplies calibrated weights via ADR-030, that *is* a Phase 1 deliverable, just done by the customer. |
| **Defer the override hierarchy too (no V1 calibration surface at all)** | Customer-supplied calibrated values would have nowhere to land. Premise of the deferral breaks. Hard reject. |
| **Delete the calibration reference document** | Information loss. Future contributors would re-derive (badly). Rejected. |
| **Treat the calibration document as V1 spec** | Misreads the PO direction; would put calibration on the V1 critical path against the explicit deferral. |

---

## Open questions

- Whether the future calibration service runs in-cluster or as a separate workload (cron job, batch service, ML pipeline). Decided when re-opened.
- Whether customer-supplied calibrated values should carry provenance metadata (who at the customer calibrated, against what data window). Likely yes; deferred to the calibration ADR.
- Whether "manual override volume sustained > 100/month" auto-files a calibration scoping ticket. Operationally yes; mechanism deferred.

---

## References

- `docs/specs/duplicate-po/calibration-methodology.md` (preserved as FUTURE reference)
- `prompts/po-spec-to-asoe.md` (HALT condition #5)
- `docs/specs/duplicate-po/2026-05-03-design-review.md` (Item 1 + decision #9)
- ADR-028, ADR-029, ADR-030, ADR-031, ADR-033
