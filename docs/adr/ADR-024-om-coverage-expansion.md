# ADR-024: Expand OM intent coverage with PRICE_HOLD_RELEASE + EDI_MISMATCH, and the PRICE_MISMATCH classifier-fork invariant

**Status:** Accepted
**Date:** 2026-04-21
**Deciders:** Principal AI Systems Architect; Architecture + Testing review panels
**Applies to:** `contracts/models.py`, `contracts/policy.py`,
`constraints/specs.py`, `constraints/fallback_backend.py`,
`recipes/registry.py`, `recipes/PriceHoldReleaseRecipe.py`,
`recipes/EdiMismatchRecipe.py`, `skills/loader.py`,
`skills/price-hold-release_SKILL.md`, `skills/edi-mismatch_SKILL.md`,
`orchestration/nodes.py`

---

## Context

The V1 foundation shipped four canonical intents —
`CONTRACTUAL_CORRECTION`, `CREDIT_BLOCK`, `MASS_PRICING_ERROR`,
`DUPLICATE_PO` — covering the pricing, credit, systemic-failure, and
duplicate-detection surfaces. A coverage audit of the Order Management
(OM) class of exceptions identified two classes that no existing
intent could express without semantic overloading:

1. **Price Hold Release.** An EDI 850 order line lands in the OMS with
   the PO price outside tolerance of the SAP base price and is *held*
   pending a release decision. The decision is different from
   CONTRACTUAL_CORRECTION: the latter *changes* the applied price via
   a pricing condition; a hold-release *decides whether to lift an
   existing block* (auto-release, escalate, or hard-reject) without
   touching the underlying price.
2. **EDI line mismatch.** An inbound EDI 850 line fails validation
   against the master record on a field other than price —
   SKU / quantity / unit-of-measure / ship-to. DUPLICATE_PO covers
   only the *duplicate* sub-type; the other mismatches had been
   implicitly bucketed under CONTRACTUAL_CORRECTION or routed to
   FAIL_TO_HUMAN, neither of which matches the real resolution path
   (buyer-notification, buyer-confirmation, or ship-to escalation).

Forcing these into the existing intent set would have (a) overloaded
semantics — a human reading a CONTRACTUAL_CORRECTION exception would
not know whether a price is being *changed* or *released*; and
(b) broken the `PriceAdjustmentRecipe` single-source-of-truth property
by introducing a second code path that also "adjusted" price.

A separate, harder question surfaced during the design review: the
**PRICE_MISMATCH** sub-type of EDI line mismatch *is* a pricing
problem — `PriceAdjustmentRecipe.py` already knows how to correct it.
Should EdiMismatchRecipe execute its own price-correction path for
this sub_type, or should it delegate?

## Decision

Two new canonical intents, two new recipes, one architectural
invariant enforced at three layers.

### Intents (contracts/models.py → `Intent`; constraints/specs.py → `AllowedIntent`)

- `PRICE_HOLD_RELEASE` — EDI 850 price-hold disposition.
- `EDI_MISMATCH` — non-duplicate, non-price EDI 850 line mismatch.

### Recipes (recipes/registry.py)

- `PriceHoldReleaseRecipe.py` — pure function, three-branch decision
  keyed on `|variance_pct|` vs policy thresholds
  (`PRICE_HOLD_TOLERANCE_PCT` = 0.02, `PRICE_HOLD_HARD_BLOCK_PCT` = 0.10).
  Output constrained by `AllowedPriceHoldAction` = {AUTO_RELEASE,
  ESCALATE, HARD_BLOCK}.
- `EdiMismatchRecipe.py` — pure classification keyed on `sub_type`.
  Accepted sub_types are constrained by `AllowedEdiMismatchSubType` =
  {SKU_MISMATCH, QTY_MISMATCH, UOM_MISMATCH, SHIP_TO_MISMATCH}. Output
  classification constrained by `AllowedEdiMismatchClassification` =
  {HARD_REJECT, REVIEW, ESCALATE}.

### Architectural invariant: PRICE_MISMATCH is routed at the classifier, not the recipe

`PRICE_MISMATCH` is **intentionally absent** from
`AllowedEdiMismatchSubType`. An EDI 850 line mismatch whose
`metadata.mismatch_sub_type == "PRICE_MISMATCH"` is classified as
`CONTRACTUAL_CORRECTION` — the pricing path — and executes
`PriceAdjustmentRecipe.py`. The invariant is enforced at three layers:

1. **Classifier** (`constraints/fallback_backend.py:classify_intent`)
   inspects `event.metadata.mismatch_sub_type` *before* returning an
   intent. A PRICE_MISMATCH event never leaves the classifier as
   `EDI_MISMATCH`.
2. **Skill loader** (`skills/loader.py:select_for_event`) mirrors the
   fork at the skill-text layer so the skill document matches the
   assigned intent — operators reviewing the exception see pricing
   guidance, not EDI-mismatch guidance.
3. **UI render** (asoe-ui `ExceptionDetailPanel` data-presence
   dispatch) renders `PriceAnalysisSection` (not `EdiMismatchSection`)
   because only `OrderAnalysis.price_analysis` is populated on these
   events; `edi_mismatch_analysis` is absent by construction.

### Rationale

- **Single source of truth for pricing (CLAUDE.md §1).** Routing the
  PRICE_MISMATCH case away from `EdiMismatchRecipe` keeps
  `PriceAdjustmentRecipe.py` as the only code path that reasons about
  pricing variance. Two recipes that both *correct* price would be
  a latent consistency bug — the next threshold or condition-type
  change would have to be mirrored in both places.
- **Recipes execute; classifiers route.** An earlier design draft
  had EdiMismatchRecipe return `FAIL_TO_HUMAN` with an explanation
  pointing at the pricing path. The architecture review flagged this
  as a §1 violation — the recipe was doing routing, which is the
  classifier's job. v2 removed it.
- **Static unreachability.** `AllowedEdiMismatchSubType` excludes
  PRICE_MISMATCH, so a pydantic `ValidationError` blocks the value
  from ever reaching the recipe even if the classifier had a bug.
  The invariant is enforced by the type system, not just by control
  flow.

## Consequences

Positive:
- OM coverage reads "Complete" rather than "Partial" in the README
  test-surface row. Each of the six OM exception classes has a
  dedicated recipe; no class is overloaded onto another.
- ML retraining pipelines (disposition audit stream per ADR-023) get
  two new `intent` keys with clean disposition semantics, not
  heterogeneous events bucketed under pre-existing intents.
- The routing fork adds a single stable invariant — "PRICE_MISMATCH
  never lands as EDI_MISMATCH" — which is testable end-to-end. A
  contract test in asoe-ui
  (`tests/e2e/edi_mismatch_data_flow.test.tsx`) asserts this and
  blocks UI regressions.

Trade-offs:
- Six intents instead of four in constrained-generation surfaces
  (Guidance regex, Outlines backend, prompts). The expansion
  required touching `fallback_backend.py`, `outlines_backend.py`,
  the sandbox prompt files, and both hand-written and test-golden
  intent vocabularies. Golden tests that hardcoded `toHaveLength(4)`
  were rewritten to `toContain(...)` so future expansions don't
  require the same sweep.
- The `validate_types` orchestration node gained two new `elif`
  arms. In the same change we added an explicit final `else` that
  FAIL_TO_HUMAN-s on an unwired-but-known recipe name — closing the
  silent-failure trap the previous fall-through represented.
  Future new intents must add their validate_types branch or be
  caught by this trap.

Out of scope for this ADR:
- SAP / Oracle / Salesforce label mapping for the new intents. That
  is a display-layer concern handled in the asoe-ui repo
  (`src/config/erp-label-map.ts`, `src/hooks/useErpProfile.ts`).
  Canonical intent codes remain the single source of truth for
  control flow, per CLAUDE.md Guardrail #1.
- Renaming existing intents to SAP-idiomatic codes
  (`CONTRACTUAL_CORRECTION` → `SD_PRICING_VARIANCE`, etc.). Discussed
  during the review; rejected in favour of the ERP label-map config
  which preserves audit-trail stability across ERP target changes.
