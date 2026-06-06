# V&V Audit Report — asoe2 (backend)

**Date:** 2026-06-06
**Scope:** Contract & semantic consistency of the order-analysis evidence
payload (recipe → adapter/composer → API schema → OpenAPI → UI).
**Method:** Static + structural cross-reference of `contracts/models.py`,
`api/schemas.py`, `api/analysis_adapters.py`, `api/analysis_composer.py`,
`recipes/`, and `orchestration/nodes.py` against the UI consumer in
`asoe-ui`. Existing tests were treated as **non-authoritative** — every
finding was reproduced from source and, where fixed, locked with a
regression test that fails on the parent commit.

Guardrail discipline applied throughout: per CLAUDE.md §6 and the
2026-04-22 Verdict, audit-bearing fields were **not** pruned to make
coverage green, recipe semantics were **not** altered, and composition was
**not** pushed onto recipes/orchestration. Fixes were limited to the
composer/adapter projection layer, which is the sanctioned assembler.

---

## FIXED

### F1 — DuplicatePO synthetic projection ignored tenant-resolved signal weights (determinism / cross-boundary value desync) — **HIGH**

**File:** `api/analysis_adapters.py` — `_synthesize_duplicate_outputs()`

**Discrepancy.** The live execution path resolves tenant-specific signal
weights via the `tenant_config` gateway (ADR-029) and plumbs them into
`detect_duplicate_po(weights=...)` at
`orchestration/nodes.py::validate_types` (line ~807:
`"weights": tenant_config.get("weights")`). The explain / shadow-gated
read path, however, re-synthesises the recipe output in
`_synthesize_duplicate_outputs()` (used whenever `resolution_data` carries
no `recommended_action`) and called `detect_duplicate_po(...)` **with no
`weights` argument** — silently falling back to the module-default
`_WEIGHTS` map.

**Root cause.** The `tenant_config` bag is present in
`record.enrichment_context` but was never read by the synthetic adapter.
Two engines (live vs. synthetic) computed the same audit-bearing
`composite_score` → `confidence`, `recommended_action`, `autonomy_applied`
from **different weight maps**, so for any tenant with non-default weights
the evidence shown to the operator diverged from the tenant's configured
policy. Demonstrated: with signals firing only `po_number` and
`line_items`, default weights yield composite `0.50` (confidence 50.0,
`REQUEST_BUYER_CONFIRMATION`); the tenant map yields composite `0.20`
(confidence 20.0) — a flipped recommendation.

**Fix.** Read `enrichment_context["tenant_config"]["weights"]` and pass it
through as `weights=`, exactly mirroring orchestration. `weights=None`
preserves the recipe's module-default fallback, so behaviour for
default-weight tenants is unchanged. No recipe semantics were touched.

**Regression test.**
`tests/test_analysis_adapters_duplicate.py::TestAdaptDuplicateSynthesisHonoursTenantWeights`
- `test_synthesis_uses_resolved_tenant_weights` — asserts confidence 20.0.
  On the parent commit this reports **50.0** (FAIL); with the fix it passes.
- `test_synthesis_falls_back_to_platform_weights_when_absent` — asserts the
  no-`tenant_config` path still yields 50.0 (default weights), matching
  orchestration's `tenant_config.get("weights") → None` behaviour.

Parent-commit verification (CLAUDE.md gate):
```
git stash push -- api/analysis_adapters.py
python -m pytest tests/test_analysis_adapters_duplicate.py::TestAdaptDuplicateSynthesisHonoursTenantWeights
# → test_synthesis_uses_resolved_tenant_weights FAILS: assert 50.0 == 20.0
git stash pop   # fix restored → 16/16 pass
```

---

## TRIAGED — NO CHANGE (with rationale)

These were investigated and found to be **by design**, **grandfathered**,
or **guardrail-protected**. Changing them would violate CLAUDE.md, so they
are recorded here rather than "fixed".

### T1 — `DuplicateDetectionData.confidence` (0–100) vs `confidence_signal.value` (0–1)
Same underlying `composite_score` carried in two units. This is intentional
and documented at `analysis_adapters.py:877` (display scaling vs. raw
signal, ADR-032). Not a disagreement — two representations of one value.
**No change.**

### T2 — `ManualOrderIntakeRecipe` echoes raw (unclamped) `composite_confidence` while deciding on the clamped value
`recipes/ManualOrderIntakeRecipe.py:206-207` clamps to `[0,1]` for the
decision but **deliberately preserves the raw value in the echo for
audit** (explicit comment). Altering the echo would change recipe
semantics (forbidden) and erase the audit trail. The correct downstream
handling is UI-side display clamping, not a recipe change. **No change.**
Logged as a UI display note in the asoe-ui report (T-UI).

### T3 — Top-level `AnalysisResponse.confidence` (intent-classifier score) vs nested `*AnalysisData` confidences (recipe scores)
These are semantically different producers (`llm_intent_classifier_raw`
vs. recipe composites) and are correctly method-tagged via
`ConfidenceSignal.method`. The provenance is explicit, not conflated.
**No change** — recommend a UI label clarifying the two are distinct.

### T4 — Preview-only / grandfathered fields that currently project as `None`
`ImpactMetrics.sla_deadline`, `AnalysisResponse.entities_analysis`,
`sap_data_analysis`, `order_entry_extraction`, `edi_850_audit`,
`change_analysis`, `knowledge_graph`, and the SAP-gateway-dependent
`OverMax/MOQ/DeliveryDelay` audit fields are explicitly marked preview-only
or grandfathered in `api/schemas.py` and tracked under
`compliance/audit_bearing_registry.yaml::grandfather_clauses`. Per
CLAUDE.md §6 these must **not** be removed to make coverage green; the
sanctioned remediation is a gateway/producer landing upstream or a
compliance-approved deadline. **No change** — gap is already tracked.

### T5 — Docstring drift (`classification` "mirrors status")
`DuplicatePORecipe.py` and `ManualOrderIntakeRecipe.py` docstrings describe
classification/status relationships that the code does not literally honour
(e.g. `classification="AUTO_BLOCK"` while `status="BLOCKED"`). Both emitted
values are valid Literals; this is documentation drift, not a contract
break. Low severity — recommend a doc touch-up, not a code change.

---

## Test execution

- `pytest -k "analysis or composer or duplicate or adapter"` → all pass.
- `tests/test_analysis_adapters_duplicate.py` → 16/16 pass with fix.
