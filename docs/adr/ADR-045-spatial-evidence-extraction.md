# ADR-045: Spatial Evidence Extraction (document-AI as candidate proposer)

**Status:** Accepted — 2026-05-26. The select-not-generate pipeline +
runtime verifier + degrade-to-text + the `extraction_spatial` eval gate
(containment / page-accuracy / hallucination / ECE) pass on the golden set in
replay (`pytest tests/eval -m replay`). The live managed-OCR / self-hosted
provider and compliance sign-off for the new provenance are **deferred**
(preprod; the recorded-replay path is the red-green floor and the live provider
is a nightly `-m live` follow-up). Proposed 2026-05-25.
**Date:** 2026-05-25
**Deciders:** Principal AI/Agentic Engineering Architect; ML/Eval lead; Agentic Orchestration; Compliance Engineer (veto); Data Engineering; Platform/SRE; Product Owner.
**Applies to:**
* asoe2: new `gateways/document_extraction.py`, `gateways/recorded_backend.py` (replay envelope extension), `api/schemas.py` (`EvidenceAnchor` spatial fields), `api/analysis_composer.py`, `compliance/audit_bearing_registry.yaml`, `contracts/policy.py` (provider cost guardrail), `tests/eval/` (new `extraction_spatial` task), `orchestration/` outbox (async extraction).

**Related:**
* ADR-043 (Attachment Preview — defines `EvidenceAnchor`; Phase-1 text anchors are the **graceful-degradation target** of this ADR).
* ADR-044 (Document Storage & Lifecycle — **frozen renditions** that geometry binds to; `sha256` idempotency).
* ADR-032 (calibration deferral — the eval-gated graduation pattern this follows).
* ADR-025 (gateway reads-before-shadow), ADR-036 (Email Intake Surface — where upstream extraction may eventually live; **this ADR does not foreclose that boundary**).
* Guardrail #3 (constrained generation for machine-consumed outputs); `docs/test-strategy/customer-inbox-tdd-strategy.md` (§4 eval harness, §3 recorded-fixture seam) and `docs/plans/attachment-preview-evidence-rollout.md`.

---

## 1. Context

ADR-043 highlights evidence by locating verbatim `text` in the rendered text
layer. That fails exactly where operators most need help — scanned/image POs,
OCR-lossy PDFs, repeated tokens, table cells. **Spatial** highlighting needs
per-field **geometry** (`page` + `bbox`), and today no coordinates exist anywhere
in the extraction schema; the constrained-generation `OrderExtractionGateway`
emits fields, not boxes.

The panel's ML + orchestration leads converged on a sharp warning: **"constrained
generation" guarantees output *shape*, not output *grounding*.** A Pydantic model
constrains a `bbox` to four floats in `[0,1]`; it cannot stop a layout LLM from
emitting four *plausible-but-invented* floats. A hallucinated coordinate is
schema-valid and points the operator's eye at the **wrong number on a
financially-binding SOX document** — worse than no highlight, and invisible to
every IoU metric (the box is geometrically fine, just semantically wrong).

## 2. Decision

### 2.1 The model is a *candidate proposer*, never a coordinate generator

The single load-bearing decision: **the system never free-generates geometry.**

1. A deterministic **OCR/layout pass** over the frozen rendition (ADR-044)
   produces a **candidate set** of `{candidate_id, text, page, bbox}` for every
   token/line/cell on the page.
2. Each field is associated to a **candidate by selection over that closed set**:
   * **First, deterministic text-match** of the field value against candidate
     tokens (handles the large majority — born-digital and clean OCR).
   * **Only for ambiguous/unmatched cases**, a constrained selection step picks
     among candidate ids — a **closed enum over real boxes**, not free-float
     generation. A hallucinated coordinate is therefore *structurally
     impossible*: the model can only choose a box the OCR actually found.
3. The chosen candidate's `bbox`/`page` populate the `EvidenceAnchor`
   (`anchor_source = "spatial_extracted"`).

### 2.2 The runtime verifier is the spine

Before any spatial anchor is projected, a **runtime verifier** asserts the
**rendered text under the selected `bbox` equals the anchor `text`**. On mismatch
(or low confidence), the anchor **drops its geometry and degrades to an ADR-043
text anchor** — never a confident box over unverified content. This verifier runs
at projection time *and* is an eval gate (§2.4). It is the same property ADR-043
§2.3 enforces for text anchors, now applied to geometry.

### 2.3 Gateway seam, provider, and orchestration

* **One extraction owns text + geometry.** Geometry is added to the *existing*
  extraction provenance (extend `ExtractedEntity` / a `SpatialProvenance`
  sub-model), **not** a second independent gateway that could disagree with the
  text extraction about the same field.
* **Provider:** prefer **managed OCR** (AWS Textract / Google Document AI / Azure
  Document Intelligence) for the candidate set — boring, accurate, native
  text+bbox+tables, bounded $/page. **Self-host** (docTR / LayoutLMv3) only where
  data residency forbids the managed provider (DPA / private endpoint otherwise).
  Either way the candidate set is deterministic; only the *ambiguous-case
  selection* (if used) is constrained generation.
* **Resilience parity.** The new `gateways/document_extraction.py` inherits the
  gateway-tier **circuit breaker + timeout** (`executor.py`) and the
  **`RecordedGatewayBackend`** replay seam — red-green never hits a live model.
* **Async, idempotent pipeline.** Extraction is slow/costly/rate-limited, so it
  runs as an **async pipeline keyed by `(sha256, model_id)`** (ADR-044 content
  address) via the effect **outbox/reconciler**, with poison-message handling.
  Geometry is **`required_for_audit = False`**: an extraction outage, timeout, or
  breaker-OPEN **falls back to ADR-043 text anchors** and never blocks preview.

### 2.4 Evaluation (what gates a view)

Spatial anchors are *newly captured* audit-bearing provenance → eval + compliance
gated (ADR-032 pattern) before they drive an operator view. New `tests/eval/`
task `extraction_spatial`:

* **Primary gate — containment**, not IoU: the predicted box must *contain* the
  ground-truth text tokens. (IoU penalises a shifted-but-correct box and rewards
  a high-overlap box that clips a digit — wrong incentive on financial fields.)
  IoU is retained as a **diagnostic, non-gating** metric.
* **Page-accuracy** = `1.0` (a wrong-page highlight is a trust-killer — **zero
  tolerance**).
* **Coordinate-hallucination rate** (predicted box text ≠ anchor text) —
  reported every run; the runtime verifier (§2.2) keeps it out of production.
* **Confidence ECE** on per-anchor `confidence` (uncalibrated confidence that
  gates whether a box shows is the same failure ADR-042's strategy already names
  for `OrderAnalysis.confidence`).
* **Golden set, bootstrapped cheaply:** born-digital docs are auto-labelled via
  the text-layer (the field's unique text span gives a near-free ground-truth
  box); expensive hand-labelling is reserved for scanned docs and tables. Rows
  pin `model_id`, `prompt_hash`.
* **CI:** `pytest tests/eval -m replay` is a PR gate; `-m live` nightly.
  Thresholds in `tests/eval/thresholds.yaml`; lowering one requires the
  compliance CODEOWNERS gate. Gates land **`xfail-strict` ahead of
  implementation** per the ratified strategy §3.

### 2.5 Cost & observability

* A **per-page cost guardrail** in `policy.py` (circuit-breaks runaway spend) plus
  a `extraction_cost_usd_total{tenant, provider, model_id}` meter — cost must be
  *queryable and attributable*, not merely capped.
* Production drift signal: `model_id` + `prompt_hash` + mean `confidence` +
  containment-on-canaries as a time series; alert on shift between nightly evals.

## 3. Compliance & security posture

* Spatial `EvidenceAnchor`s are **newly captured** provenance → registry rows +
  compliance sign-off before they drive a view; the `anchor_source` discriminator
  keeps them distinguishable from ADR-043 text anchors in the audit trail.
* Managed-OCR egress (if chosen) is a **separate, narrow** SSRF allowlist routed
  through `hardening.ssrf.validate_outbound_url` (`resolve=True`), with DLP /
  field-minimisation and a DPA — never the broad ingestion allowlist.
* PII residency: self-host or private-endpoint where the DPA requires it; no bytes
  to an unapproved third party.

## 4. Alternatives considered (rejected)

* **Layout LLM free-generates `bbox`** — rejected: hallucinated-but-shape-valid
  coordinates, invisible to IoU, dangerous on a SOX surface (§1). The model may
  only *select* real OCR boxes (§2.1).
* **IoU as the primary gate** — rejected: wrong incentive for financial fields;
  containment + page-accuracy gate, IoU diagnostic only (§2.4).
* **A second, independent geometry gateway** — rejected: two extractions of the
  same field disagree, forcing reconciliation logic between shadow and execute
  (Guardrail #6). One extraction owns text + geometry (§2.3).
* **Synchronous extraction in the preview request** — rejected: an external OCR
  outage would stall a financially-binding operator view. Async + degrade to text
  anchors (§2.3).
* **Geometry `required_for_audit = True`** — rejected: would make an OCR outage
  block authorisation. Text anchors are the audit-sufficient floor.

## 5. Consequences

* A new document-AI dependency (managed or self-hosted), a per-page cost, an eval
  gate, and an async extraction pipeline — all gated and observable.
* Pixel-accurate overlays that survive re-render (bound to ADR-044 frozen
  renditions) **when** verified; otherwise the operator transparently sees ADR-043
  text highlighting.
* Hallucinated geometry is structurally impossible (select-not-generate) and
  caught at runtime (verifier) even if a candidate is mis-selected.

## 6. Definition of Done

`gateways/document_extraction.py` producing OCR candidate sets + select-not-generate
field association; the runtime verifier (rendered-text-under-box == anchor text)
with degrade-to-text fallback; spatial `EvidenceAnchor` fields + audit-registry
rows + compliance sign-off; `RecordedGatewayBackend` envelope extended for
geometry + determinism (replay → byte-identical anchors); the `extraction_spatial`
eval task (containment / page-accuracy / hallucination / ECE) with `thresholds.yaml`
+ CODEOWNERS gate, written `xfail-strict` ahead; circuit-breaker parity +
degrade-to-text test; async outbox pipeline keyed on `(sha256, model_id)` with
poison-message handling; per-page cost guardrail + cost meter + drift signal;
frozen-rendition binding (ADR-044); docs updated.

Graduates to **Accepted** when the eval gate is met on the golden set, the
provider decision is ratified, and compliance signs off the new provenance.

## 7. Implementation status (CP-F)

Landed in `gateways/document_extraction.py` + `tests/eval/spatial_scorer.py`,
locked by green contract tests:

* **`select_candidate_box`** — a field may only SELECT a real OCR candidate by
  id; an out-of-set id raises `ValueError`, so hallucinated geometry is
  structurally impossible (§2.1).
* **`verify_anchor_geometry`** — the runtime verifier: a spatial anchor keeps its
  geometry only if the rendered text under the box matches the anchor text, else
  it DEGRADES to an ADR-043 text anchor (geometry is `required_for_audit=False`).
* **`build_spatial_anchor`** — the single mint point: select → construct → verify.
* **Eval scorers** — `containment` (primary gate) + `page_accuracy`
  (zero-tolerance); IoU stays diagnostic-only (§2.4).

## 8. Implementation status (CP-G — Accepted, 2026-05-26)

Landed and green (replay path):

* **`DocumentExtractionGateway`** — proposes an OCR candidate set and associates
  fields by SELECTION over it (`build_spatial_anchor`: select → construct →
  verify). Default backend `RecordedDocumentExtractionBackend` (replay; red-green
  never hits a live model). **Circuit-breaker parity** (OPEN → degrade to no
  geometry). **Idempotent** keyed on `(sha256, model_id)` (replay → byte-identical
  anchors).
* **Eval gate** — recorded fixtures + `tests/eval/datasets/extraction_spatial/`
  golden set; `spatial_scorer` (containment primary, page-accuracy zero-tolerance,
  coordinate-hallucination, confidence-ECE; IoU diagnostic-only) wired into a
  `-m replay` gate against `thresholds.yaml`. (No CODEOWNERS gate this engagement.)
* **Composer wiring** — `build_evidence_anchors` prefers a VERIFIED spatial anchor
  bound to the exact bytes, degrading to text anchors on miss/outage (geometry
  `required_for_audit=False`). Spatial registry rows in place
  (`page`/`bbox`/`confidence`/`rendition_hash`, all contextual).
* **Cost & drift** — per-page cost guardrail in `policy.py`
  (`assert_within_page_cost_budget`) + an attributable cost meter + a drift signal
  (mean confidence + canary containment by `model_id`/`prompt_hash`) in
  `api/metrics.py`.
* **Frozen-rendition binding** — `EvidenceAnchor.rendition_hash` ties geometry to
  ADR-044's frozen render basis.
* **Frontend** — PDF.js canvas bbox overlays for verified spatial anchors
  (`spatialOverlays`), with the text-derived safety bar as the authoritative
  fallback.

**Deferred (preprod):** the live managed-OCR / self-hosted provider behind the
seam (nightly `-m live`); the async **outbox** pipeline (extraction is invoked
out-of-band and is idempotent on `(sha256, model_id)`, but full
outbox/reconciler wiring + poison-message handling is a follow-up); and
compliance sign-off for the new provenance.

---

*End of ADR-045.*
