# ADR-043: Attachment Preview & In-Document Evidence Highlighting (Phase 1)

**Status:** Proposed — revised 2026-05-25 after the 11-expert joint review (Round 3 convergence).
**Date:** 2026-05-25
**Deciders:** Principal AI/Agentic Engineering Architect; Frontend Platform; Compliance Engineer (veto); Security Engineer; UX Architect; Product Owner.
**Applies to:**
* asoe2: `api/schemas.py` (new `EvidenceAnchor` projection + closed `supports_*` vocabulary), `api/analysis_composer.py` / `api/analysis_adapters.py`, `api/routes/attachments.py` (new sandboxed preview surface), `compliance/audit_bearing_registry.yaml`, `api/metrics.py` (highlight/preview SLIs), `contracts/policy.py`.
* asoe-ui: `src/app/exceptions/EmailSourceSection.tsx`, a new `AttachmentPreview` surface under `src/components/ui/`, `src/lib/api.ts`, `src/types/exceptions.ts`.

**Related:**
* ADR-034 (email-order-entry skill — extraction + `source_span` provenance).
* ADR-036 (Email Intake Surface — *Proposed*; the upstream email-intelligence-agent / attachment ingestion this builds on). **ADR-043 does not decide where spatial provenance is produced — that is ADR-036/ADR-045's call.**
* ADR-042 (Customer-Inbox port — `EMAIL_ENTRY` lens, the analysis payload + dumb-projector sections; the attachment store + download endpoint landed under DoR #10 / PR #169).
* **ADR-044 (Document Storage & Lifecycle)** — object storage, retention/TTL, right-to-erasure, frozen renditions. **GA precondition for this preview.**
* **ADR-045 (Spatial Evidence Extraction)** — the document-AI layer that fills spatial `EvidenceAnchor` geometry. Phase 2 of this feature lives there.
* ADR-025 (gateway reads before shadow), ADR-031 (read-projection split), ADR-032 (calibration deferral — eval-gated graduation).
* `docs/test-strategy/customer-inbox-tdd-strategy.md` (the ratified TDD/BDD strategy this feature extends) and `docs/plans/attachment-preview-evidence-rollout.md` (execution plan + test-first backlog).

---

## 0. What changed in this revision

The original ADR-043 bundled three decisions (preview, blob storage at scale,
spatial document-AI) into one document. The 11-expert panel converged on
**splitting them** so each is independently reviewable and gated:

* **ADR-043 (this doc)** — **Phase 1 only**: the sandboxed preview + the
  text-derived `EvidenceAnchor` contract + the **highlight safety bar** that the
  panel made non-negotiable.
* **ADR-044** — document storage & lifecycle (get blobs out of the primary OLTP
  DB; retention/erasure; frozen renditions). A GA precondition.
* **ADR-045** — spatial extraction (document-AI as a *candidate proposer*, never
  a coordinate generator).

**Product Owner decisions carried in (2026-05-25):** (1) the preview highlights
**all** extracted evidence fields by default, not only the decision-driving one;
(2) Phase 2 (spatial) is planned now — see ADR-045; (3) the three-ADR split is
adopted.

> Because we highlight *every* field by default (PO decision 1), the panel's
> automation-bias and wrong/missing-highlight concerns bind **harder**, not
> softer. The Safety Bar (§2.3) and the decision-quality gate (§2.7) are
> therefore mandatory Phase-1 scope, not polish.

## 1. Context

The attachment store (DoR #10, PR #169) persists inbound email attachment bytes
(`V016 email_attachment`; SQLite/Postgres) and serves them via
`GET /api/v1/cases/{case_id}/attachments/{attachment_id}`, tenant- + RBAC-gated,
**forced-download** (`Content-Disposition: attachment` + `nosniff`). The
`EmailAttachmentManifestEntry` carries `name`, `mime_type`, `bytes`, `sha256`,
`attachment_id`.

**Product Owner request:** clicking an attachment should open a **preview
window** in which **the evidence relevant to the case and intent is highlighted
inside the document** (the PO number, line items, ship-to, and the values that
drove the Change Analysis decision are called out on the rendered PO).

This is aligned with the system's evidence-provenance ethos — the operator
authorises a financially-binding, SOX-relevant decision against the source
document — but it crosses three boundaries that need a deliberate decision:

* **Security.** The download path is *forced-download, never inline* by design,
  precisely to keep untrusted attachment bytes from rendering/executing in the
  app origin. A preview reverses that — we must render attacker-supplied content.
  The panel's lead finding here: specify the controls as **tested invariants**,
  and close the **data-exfiltration** axis (`connect-src 'none'`), not only the
  script-execution axis.
* **Provenance.** Highlighting a region *inside* a rendered document needs an
  anchor. Today the only provenance is `ExtractedEntity.source_span` (the
  **verbatim text** a value came from) — there are **no spatial coordinates**.
* **Architecture.** The UI is a dumb projector; `build_analysis` + the composer
  are the sole assemblers (Guardrail #6). Highlight anchors must be
  **backend-authoritative** — the UI may *locate and render* known evidence, it
  may not *search for and invent* evidence client-side on a SOX surface.

## 2. Decision (Phase 1)

Build a **sandboxed, format-specific preview** that renders the attachment and
draws **backend-authoritative, text-derived highlights** for every extracted
evidence field, under a strict safety bar. **No new AI dependency** — Phase 1
reuses data we already capture (`source_span` + the order-entry / change-analysis
fields). Spatial geometry is ADR-045; blob-storage-at-scale is ADR-044.

### 2.1 Sandboxed, format-specific preview — security as tested invariants

Never render untrusted bytes inline in the app origin. A per-format viewer,
served from an **opaque/sandboxed origin**, selected by **validated magic
bytes** (default-deny), with the following invariants — each backed by a failing
test before code (see §2.6, plan §test-backlog):

| Format | Renderer | Notes |
|---|---|---|
| PDF | PDF.js → canvas + text layer | `isEvalSupported: false`; scripting / annotations / XFA / JS-actions **disabled**; worker bundled (no CDN), version-pinned + SRI |
| Image (png/jpg/webp/gif) | `<img>` from a scoped blob | magic-byte verified; **SVG denied** |
| text / CSV | escaped text / table render | no HTML interpretation |
| Office (docx/xlsx) | **not in Phase 1** — server-side convert (ADR-044/045) | — |

Security invariants (testable):

* **iframe `sandbox`** = `allow-scripts` only (PDF.js worker). **Never**
  `allow-same-origin` (the `allow-scripts`+`allow-same-origin` combination is a
  documented escape and is forbidden by test), nor `allow-top-navigation`,
  `allow-popups`, `allow-forms`, `allow-modals`.
* **CSP on the preview document** (the exfiltration lockdown):
  `default-src 'none'; script-src 'self'` (bundled PDF.js, **no** `unsafe-inline`
  / `unsafe-eval`); `img-src` scoped to the preview blob; `style-src 'self'`;
  `font-src 'self'`; **`connect-src 'none'`**; `object-src 'none'`;
  `frame-ancestors 'self'`; `base-uri 'none'`; `form-action 'none'`.
* **Magic-byte selection is necessary, not sufficient.** It prevents renderer
  mis-selection; it does **not** stop polyglots — so scripting stays disabled and
  the sandbox stands regardless. Default-deny: anything not in the allowlist is
  download-only.
* **Same authorization as download.** Preview reads through the existing
  tenant + RBAC + case-scoped check (`attachments.py`); an explicit **IDOR
  abuse-case test** (cross-tenant / wrong-`case_id` `attachment_id`) is a gate.
* **Dependency currency** is a *continuous* SCA gate on PDF.js, not a one-time
  review note.
* No attachment bytes leave the tenant (no third-party render service).

The forced-download endpoint is unchanged; preview is **additive and
sandboxed**.

### 2.2 The `EvidenceAnchor` contract (backend-authoritative)

The composer projects an `EvidenceAnchor` list onto the analysis payload — the
single backend-authoritative source the viewer renders. One schema spans both
phases via a discriminator; **Phase-1 geometry is structurally absent and
tripwire-locked** so no Phase-1 code path can fabricate coordinates.

```
EvidenceAnchor:
  attachment_id: str
  anchor_source: Literal["text_derived", "spatial_extracted"]  # discriminator; Phase 1 == "text_derived"
  text: str                       # the verbatim evidence (from source_span / the field value)
  match_key: MatchKey             # backend-supplied deterministic locator (see §2.4)
  supports_kind: EvidenceKind     # CLOSED enum (not a free string)
  supports_ref: str               # value from a CLOSED, parity-locked vocabulary
  label: str                      # operator-facing ("PO number", "Quantity that breached ATP")
  source_sha256: str              # binds the anchor to the EXACT attachment bytes
  # spatial fields — ADR-045; MUST be None when anchor_source == "text_derived":
  page: Optional[int] = None
  bbox: Optional[list[float]] = None        # [x0,y0,x1,y1] normalised
  confidence: Optional[float] = None
```

* **`supports_kind` / `supports_ref` are a closed, backend-owned vocabulary**
  (e.g. `EvidenceKind = extracted_field | constraint | decision`; refs like
  `order_entry.po_number`). The UI maps them with a `default` fallback (the
  `verdictVariant()` pattern) so **adding a highlightable field is a backend-only
  change with zero UI edits**. A Py↔TS parity lock guards the vocabulary.
* **`source_sha256`** binds every anchor to a specific byte snapshot, so an
  attachment re-version can never silently re-point an authorised highlight.
* **All extracted fields are projected** (PO decision 1): order-entry fields,
  change-analysis constraints, and the decision driver each become an anchor,
  labelled with what it supports.
* `EvidenceAnchor` is placed on the analysis payload and rides the **OpenAPI→TS
  round-trip gate from day one** (all spatial fields null in Phase 1), freezing
  the contract before ADR-045 fills it.

### 2.3 The highlight safety bar (mandatory — the panel's #1 finding)

Five independent reviewers converged on the dominant hazard: on a SOX surface, a
highlight that lands on the **wrong** occurrence — or **silently fails** to
land — is *worse than none*, because it manufactures false operator confidence.
The following are Phase-1 acceptance criteria:

* **Per-anchor render outcome** is one of `LOCATED | UNLOCATED | AMBIGUOUS`,
  computed by a **runtime verifier** at render time:
  * `LOCATED` — the anchor `text` resolves to exactly the span identified by
    `match_key` in the rendered text layer.
  * `AMBIGUOUS` — `text` matches more than the expected occurrence; shown as
    "position approximate", never silently boxed on a guess.
  * `UNLOCATED` — `text` cannot be found (scanned/image PDF, OCR loss, line
    breaks). **Shown as loudly as a hit** ("value not found in this document —
    verify manually"); the value text + download remain available. Never a
    silent absence.
* **Runtime semantic verification:** a highlight is only drawn where the rendered
  text under it **equals** the anchor `text`. A mismatch downgrades to
  `UNLOCATED` — the viewer never draws a confident box over unverified content.
* **The audit-authoritative unit is `(attachment_id, source_sha256, text,
  supports_ref)`**, explicitly **decoupled from on-screen position**. The audit
  hash covers the evidence tuple, *never* `page`/`bbox`/pixel coordinates. The
  on-screen position is a best-effort convenience and is labelled as such.

### 2.4 Where text-matching happens (Guardrail #6 preserved)

To keep the UI a dumb projector, the **backend** supplies a deterministic
`MatchKey` (`{normalized_text, occurrence_index}`) computed when the anchor is
projected. The **UI performs a pure, literal locate** against that key in the
rendered text layer and reports the outcome (§2.3) — it does **no** fuzzy
search, ranking, or "find evidence" logic. The matching *policy* is
backend-owned and unit-tested; the UI only positions and reports.

### 2.5 Wiring (no projector violation)

* `case_id` is threaded as an **explicit prop**
  (`ExceptionDetailPanel → EmailSourceSection → AttachmentPreview`) — **not**
  React context (provenance must be visible on a SOX surface).
* **Bytes** are fetched via an explicit, RBAC-gated method in `src/lib/api.ts`;
  the section never calls `fetch()` directly.
* **Anchors** arrive via `analysis.*` (the composer projection), the same path
  `attachment_manifest` already takes.

### 2.6 Observability (Phase-1 Definition of Done)

The feature is not done until it is debuggable in production:

* `highlight_outcome_total{result=located|unlocated|ambiguous, mime}` and a
  **match-hit-ratio** SLI (the leading indicator that a PDF.js bump silently
  broke positioning).
* `preview_render_total{result, mime}` and a **preview-latency** SLO histogram
  (byte-fetch → first render).
* A **zero-highlight alert**: `anchor_count > 0 ∧ located == 0`.
* **Bounded cardinality** — Prometheus labels limited to `{result, mime}`;
  high-cardinality fields (`attachment_id`, `case_id`, page counts) go to the
  structured per-preview event / trace, never to label sets.

### 2.7 Decision-quality gate (automation bias)

Because every field is highlighted by default, the feature must demonstrably make
the operator's decision *better*, not just faster:

* Wire a **`highlight_shown`** dimension into the existing automation-bias SLIs
  (§6 #11: override rate, Layer-2-open rate, decision dwell), so a *drop in
  scrutiny* registers as a regression, not a win.
* A **non-dismissable** preview banner: *"Highlights mark where the system
  believes evidence appears. Absence of a highlight is not confirmation a value
  is absent. You are authorising against the document."*
* **Pre-GA A/B** on a labelled set seeded with deliberately wrong/missing
  highlights, measuring operator catch-rate. This is the product gate, separate
  from render correctness.

## 3. Compliance & security posture

* **Audit-bearing.** `EvidenceAnchor` is registered in
  `audit_bearing_registry.yaml`. Phase-1 anchors are *derived* from existing
  `source_span` (no new capture, no grandfather clause). The
  `anchor_source` discriminator records the provenance regime, so an auditor can
  always tell a text-derived highlight from a future spatial one (ADR-045).
* **Security review required** for the inline-render surface; the invariants in
  §2.1 are the review checklist and the test list.
* **PII.** Preview *renders* customer documents. Encryption-at-rest,
  retention/TTL, no client-side byte caching beyond the session, and
  in-tenant-only handling are owned by **ADR-044** and are a **GA precondition**.

## 4. Alternatives considered (rejected)

* **Client-side "find evidence" search** with no backend anchors — client-side
  business logic on a SOX surface; non-deterministic; violates Guardrail #6.
* **UI does fuzzy text-matching** — rejected in favour of a backend-supplied
  deterministic `MatchKey` + pure literal locate (§2.4).
* **Two `EvidenceAnchor` types (text vs spatial)** — rejected in favour of one
  schema + an `anchor_source` discriminator + a tripwire that Phase-1 never sets
  geometry (avoids two near-duplicate models while keeping the safety property).
* **Open attachment in a new tab (native render)** — full XSS surface, no
  highlight capability.
* **Third-party document viewer** — sends customer PII to a third party.
* **Build spatial highlighting now in this ADR** — split to ADR-045; no
  coordinates exist today and the model must *select* boxes, not generate them.

## 5. Consequences

* Reuses the attachment store + download (PR #169) as the byte source; the
  manifest `attachment_id` + `sha256` are the preview/highlight keys.
* Phase 1 is shippable soon and folds in the previously-deferred **download
  button**.
* The app gains an inline-render surface — a new, reviewable, **tested** security
  boundary.
* `EvidenceAnchor` becomes a frozen cross-language contract (Py↔TS) ready for
  ADR-045 to fill spatially.
* Two GA preconditions are now explicit and owned elsewhere: **storage**
  (ADR-044) and **PII/retention** (ADR-044).

## 6. Definition of Done (Phase 1)

Sandboxed viewer (pdf/img/text/csv) behind tenant + RBAC + case scope;
magic-byte default-deny format selection; the §2.1 sandbox/CSP invariants with
their abuse-case tests green; composer-projected text `EvidenceAnchor`s for all
fields with the closed `supports_*` vocabulary + Py↔TS parity lock; the §2.3
safety bar (LOCATED/UNLOCATED/AMBIGUOUS + runtime verifier + audit-tuple
decoupled from position); backend `MatchKey` + UI literal locate; explicit
`case_id` prop wiring; §2.6 observability series + SLO; §2.7 banner + SLI wiring;
storage-portability contract test landed (impl in ADR-044); download button;
axe + component + mock-preview tests; docs updated.

This ADR moves to **Accepted** when Phase 1 ships green against the above and the
security review signs off the inline-render surface.

---

*End of ADR-043 (Phase 1). Spatial highlighting → ADR-045. Storage & lifecycle → ADR-044.*
