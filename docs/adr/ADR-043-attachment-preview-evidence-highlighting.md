# ADR-043: Attachment Preview & In-Document Evidence Highlighting

**Status:** Proposed (2026-05-25)
**Date:** 2026-05-25
**Deciders:** Principal AI/Agentic Engineering Architect; Frontend Platform; Compliance Engineer (veto); Security Engineer; UX Architect; Product Owner.
**Applies to:**
* asoe2: `gateways/attachment_store.py`, `api/routes/attachments.py`, `api/schemas.py` (new `EvidenceAnchor` projection), `api/analysis_composer.py` / `api/analysis_adapters.py`, `compliance/audit_bearing_registry.yaml`, a future `gateways/document_extraction.py`, `contracts/policy.py`.
* asoe-ui: `src/app/exceptions/EmailSourceSection.tsx`, a new `AttachmentPreview` surface under `src/components/ui/`, `src/lib/api.ts`, `src/types/exceptions.ts`.

**Related:**
* ADR-034 (email-order-entry skill — extraction + `source_span` provenance).
* ADR-036 (Email Intake Surface — *Proposed*; the upstream email-intelligence-agent / attachment ingestion this builds on).
* ADR-042 (Customer-Inbox port — `EMAIL_ENTRY` lens, the analysis payload + dumb-projector sections; the attachment store + download endpoint landed under DoR #10 / PR #169).
* ADR-025 (gateway reads before shadow), ADR-031 (read-projection split), ADR-032 (calibration deferral — eval-gated graduation).

---

## 1. Context

The attachment store (DoR #10, PR #169) persists inbound email attachment bytes
in ASOE's own DB (V016 `email_attachment`; SQLite/Postgres) and serves them via
`GET /api/v1/cases/{case_id}/attachments/{attachment_id}`, tenant- + RBAC-gated,
forced-download (`Content-Disposition: attachment` + `nosniff`). The
`EmailAttachmentManifestEntry` carries `name`, `mime_type`, `bytes`, `sha256`,
`attachment_id`.

**Product Owner request:** clicking an attachment should open a **preview
window** in which **the evidence relevant to the case and intent is highlighted
inside the document** (e.g. the PO number, line items, ship-to, and the values
that drove the Change Analysis decision are boxed/called out on the rendered PO).

This is well-aligned with the system's evidence-provenance ethos — the operator
authorises a financially-binding, SOX-relevant decision against the source
document. But it crosses two boundaries that need a deliberate decision:

* **Security.** The download path is *forced-download, never inline* by design,
  precisely to keep untrusted attachment bytes from rendering/executing in the
  app origin (XSS). A preview reverses that — we must render attacker-supplied
  content.
* **Provenance.** Highlighting a region *inside* a rendered document needs an
  anchor. Today the only provenance captured is `ExtractedEntity.source_span`
  (the **verbatim text** a value came from) — there are **no spatial
  coordinates** (page / bounding box) anywhere in the extraction schema.
* **Architecture.** The UI is a dumb projector; `build_analysis` + the composer
  are the sole assemblers (Guardrail #6). Highlight anchors must be
  **backend-authoritative** — the UI may *locate and render* known evidence, it
  may not *search for and invent* evidence client-side on a SOX surface.

## 2. Decision

Build the feature as **two capabilities, delivered in two phases**, reusing the
attachment store + download as the byte source.

### 2.1 Capability A — sandboxed, format-specific preview

Never render untrusted bytes inline in the app origin. A per-format viewer:

| Format | Renderer | Isolation |
|---|---|---|
| PDF | PDF.js → canvas + text layer | sandboxed `<iframe>` (no `allow-same-origin` to the app origin), strict per-preview CSP |
| Image (png/jpg/webp/gif) | `<img>` | CSP `img-src` scoped to the preview blob; magic-byte check vs declared `mime_type` |
| text / CSV | escaped text / table render (no HTML interpretation) | n/a |
| Office (docx/xlsx) | **Phase 2** — server-side convert to PDF/images | no native browser render |

* The renderer is selected by **validated magic bytes**, not the
  attacker-supplied `mime_type`.
* The preview reads through the **same tenant + RBAC + case-scoped** check as the
  download; the forced-download endpoint stays as-is, and preview is a separate,
  explicitly-sandboxed surface.
* No attachment bytes are sent to any third-party render service (PII).

### 2.2 Capability B — evidence highlighting (backend-authoritative anchors)

The composer projects an **`EvidenceAnchor`** list onto the analysis payload —
the single backend-authoritative source the viewer renders. Shape:

```
EvidenceAnchor:
  attachment_id: str          # which document
  text: str                   # the verbatim evidence (from source_span)
  supports_kind: str          # "extracted_field" | "constraint" | "decision"
  supports_ref: str           # e.g. "order_entry.po_number", "change_analysis.constraint.inventory"
  label: str                  # operator-facing ("PO number", "Quantity that breached ATP")
  # Phase 2 spatial fields (None in Phase 1):
  page: Optional[int]
  bbox: Optional[list[float]] # [x0, y0, x1, y1] normalised
  confidence: Optional[float]
```

* **Phase 1 (text-level):** anchors are *derived from data we already have* —
  `ExtractedEntity.source_span` and the order-entry / change-analysis fields —
  with `page`/`bbox` = None. The viewer highlights by **locating the
  backend-provided `text` in the rendered text layer** (PDF.js text layer / CSV
  cells), each highlight labelled with the field/decision it supports. The
  *evidence* is authoritative; the *on-screen position* is a best-effort
  convenience (and is stated as such in the UI). Guardrail #6 holds — the UI
  locates known evidence, it does not invent it.
* **Phase 2 (spatial):** a new **layout-aware document-extraction gateway**
  emits per-field provenance with coordinates; the composer fills `page`/`bbox`;
  the viewer draws exact overlays that survive re-render and don't depend on a
  text match.

### 2.3 New capability required for Phase 2

Spatial anchors require a **document-AI extraction layer** that returns text
*and* geometry. The current constrained-generation extraction
(`OrderExtractionGateway`) emits fields, not coordinates. Options (a follow-on
decision in Phase 2):

| Option | Data residency | Ops | Notes |
|---|---|---|---|
| AWS Textract / Google Document AI / Azure Document Intelligence | leaves tenant (DPA / private endpoint needed) | low | managed text+bbox+tables, per-page cost |
| Self-hosted layout model (docTR / LayoutLMv3 / layout LLM via Outlines) | stays in-tenant | higher | integrates via the existing gateway + constrained-gen seam |

Machine-consumed coordinate/field output MUST be constrained + schema-validated
(Guardrail #3), and graduate via the eval harness (ADR-032 pattern): field
accuracy + bounding-box IoU on a golden set before it gates an operator view.

## 3. Phased delivery

| Phase | Scope | Gated by |
|---|---|---|
| **1** | Sandboxed preview (pdf/img/text/csv) + composer-projected **text** `EvidenceAnchor`s + highlight-by-text with field/decision linkage + the deferred **download button** (folds in here) | CSP/sandbox security test; magic-byte validation test; axe; component + mock-mode preview tests |
| **2** | Document-AI extraction gateway (constrained + validated coordinates) → **spatial** `EvidenceAnchor`s; office→PDF conversion; pixel-accurate overlays | eval harness (field accuracy + bbox IoU); provider cost guardrail (`policy.py`); compliance sign-off on the new audit-bearing provenance; PII/retention policy shipped |

Phase 1 builds entirely on PR #169 with **no new AI dependency**; it delivers the
bulk of the PO's intent. Phase 2 is a genuine project (provider selection, cost,
eval, compliance) and is ADR-gated by this document.

## 4. Compliance & security posture

* **Audit-bearing.** `EvidenceAnchor` is the evidence the operator authorises
  against → registered in `audit_bearing_registry.yaml`. Phase-1 anchors are
  *derived* from existing `source_span` (no new capture, no new grandfather
  clause). Phase-2 spatial anchors are *newly captured* provenance → CODEOWNERS /
  compliance gate + eval before they drive a view.
* **Security review required** for the inline-render surface (sandbox escape, CSP,
  magic-byte spoofing, PDF.js CVE currency). The forced-download endpoint is
  unchanged; preview is additive and sandboxed.
* **PII / retention (carried from the store ADR).** Preview *renders* customer
  documents, which makes the deferred PII work non-optional for GA: encryption
  at rest, retention/TTL, no client-side caching of bytes beyond the session, and
  in-tenant-only conversion. Tracked as a Phase-2 precondition.

## 5. Alternatives considered (rejected)

* **Client-side "find evidence" search highlighting** with no backend anchors —
  client-side business logic on a SOX surface; non-deterministic; violates
  Guardrail #6.
* **Open attachment in a new browser tab (native render)** — full XSS surface and
  no highlight capability.
* **Third-party document viewer** (e.g. hosted Google/Office viewers) — sends
  customer PII to a third party; rejected.
* **Build spatial highlighting now** — not possible: no coordinates exist; would
  require faking geometry.

## 6. Consequences

* Reuses the attachment store + download (PR #169) as the byte source; the
  manifest's `attachment_id` is the preview/highlight key.
* Phase 1 is shippable soon and includes the previously-deferred download button.
* The app gains an inline-render surface — a new, reviewable security boundary.
* Phase 2 introduces an external/again self-hosted document-AI dependency, a
  per-page cost, an eval gate, and makes PII/retention a hard GA blocker.

## 7. Definition of Done

**Phase 1** — sandboxed viewer (pdf/img/text/csv) behind tenant + RBAC + case
scope; magic-byte format selection; composer-projected text `EvidenceAnchor`s
with field/decision linkage; highlight-by-text in the viewer; download button;
CSP/sandbox + magic-byte + axe + component + mock-preview tests; docs updated.

**Phase 2** — document-extraction gateway with constrained + validated coordinate
output; spatial `EvidenceAnchor` schema + audit-registry rows + eval harness
(accuracy + bbox IoU) + compliance sign-off; office→PDF conversion; pixel
overlays; PII retention policy shipped. This ADR moves to **Accepted** when
Phase 1 ships and the Phase-2 provider decision + eval gate are ratified.

---

*End of ADR-043.*
