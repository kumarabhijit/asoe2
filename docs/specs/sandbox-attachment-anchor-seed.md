# Spec: Sandbox attachment + evidence-anchor seed endpoint

**Status:** Proposed (preprod test fixture)
**Owner:** to be implemented in the "finish pending tasks" session, Priority-1 step 1.
**Applies to:** `asoe2/api/routes/sandbox.py` (new endpoint), `tests/test_sandbox_routes.py` (new tests).
**Related:** ADR-043 (preview + evidence highlighting), ADR-036 (Email Intake Surface — the *real* producer this stands in for), `tests/browser/attachment-evidence.spec.ts` (the Playwright journeys this unblocks).

---

## 1. Why this exists

The ADR-043 operator journeys and a preprod demo both need an `EMAIL_ENTRY`
case that has **a stored attachment + projected `EvidenceAnchor`s** so the
preview safety bar shows *located* highlights. In production that data is
produced by the email-intelligence-agent ingestion (ADR-036, unbuilt). This is a
**sandbox-only stand-in** that produces the *same shape* ADR-036 will
(`enrichment_context.email_source_context` + `extracted_entities` + a stored
blob), so the existing composer path (`adapt_email_source` →
`build_evidence_anchors`) derives anchors with **no new business logic**.

This is **not** ADR-036. It is a test seam, double-gated to `ASOE_ENV=sandbox`,
and is replaced by the real producer for non-test flows when ADR-036 ships.

## 2. Endpoint

```
POST /api/v1/_sandbox/seed/email-attachment-anchors
```

* **Guards (mirror the existing sandbox routes):** router mounted only when
  `ASOE_ENV=sandbox`; handler calls `_require_sandbox()`;
  `dependencies=[Depends(require_role("manager", "admin"))]`; tenant from
  `Depends(get_tenant_id)`.
* **Request** (Pydantic, `extra="forbid"`). Use **snake_case**; update the
  Playwright spec to match (it currently sends `documentText`/`attachmentName`/
  `attachmentMime` — reconcile to snake_case, or add pydantic `alias`es; pick one
  and keep both sides consistent):

  ```
  document_text: str            # the text the attachment's rendered layer contains
  attachment_name: str          # e.g. "PO_8842.pdf"
  attachment_mime: str          # "application/pdf" | "text/csv" | "text/plain"
  anchors: list[AnchorSpec]     # AnchorSpec = { text: str, label: str, supports_ref: str }
  subject: str | None = None        # sensible default if absent
  from_address: str | None = None   # sensible default if absent
  ```

* **Response:** `{ "exception_id": str, "case_id": str, "attachment_id": str, "ok": true }`
* **Errors:** `403` outside `ASOE_ENV=sandbox`; `422` on validation.

## 3. Behaviour

1. `_require_sandbox()`.
2. **Build attachment bytes that CONTAIN `document_text`** (so the real text
   layer is extractable and anchors locate):
   * `application/pdf` → generate a minimal, openable one-page PDF embedding
     `document_text` — a Python `_mock_pdf_bytes(text: str) -> bytes` helper that
     mirrors `asoe-ui/src/lib/mock-data/attachment-bytes.ts::makeMinimalPdf`
     (one text run per line, ASCII, valid `xref`/`%%EOF`).
   * `text/csv` / `text/plain` → `document_text.encode("utf-8")`.
   * otherwise → `document_text.encode("utf-8")` (still downloadable; preview
     default-denies non-allowlisted types, which is correct).
3. **Create an `EMAIL_ENTRY` exception + parent case** by reusing the existing
   `/_sandbox/seed/manual-order-intake` path (or emit a `MANUAL_ORDER_INTAKE`
   event through the normal `/exceptions/resolve` flow). Capture `exception_id`
   and `case_id` (the record's `parent_case_id`).
4. `store_attachment(tenant_id, name=attachment_name, mime_type=attachment_mime,
   content=<bytes>, case_id=case_id)` → `AttachmentRecord` (gives `id`, `sha256`).
5. **Stamp `enrichment_context`** on the record (`exception_store.update(...,
   enrichment_context=...)`) so the composer derives anchors:

   ```
   email_source_context = {
     from_address, received_at (now, ISO-8601), subject,
     body_hash: sha256(document_text),
     attachment_manifest: [{
       name: attachment_name, mime_type: attachment_mime,
       bytes: <size>, sha256: rec.sha256, attachment_id: rec.id,   # BOTH required
     }],
     body_excerpt: document_text[:240],
   }
   extracted_entities = [                                           # one per anchor
     { key: supports_ref.rsplit(".", 1)[-1], value: a.text,
       kind: "field", source_span: a.text }
     for a in anchors
   ]
   ```

   `build_evidence_anchors(record)` reads `attachment_manifest` (needs
   `sha256` + `attachment_id`) + `extracted_entities` and emits `text_derived`
   anchors bound to the stored attachment, with `match_key` normalised — so
   `analysis.email_source.evidence_anchors` is populated and locates against
   `document_text`.
6. Return the ids.

**Data flow that makes the UI light up:** `adapt_email_source` projects
`email_source_context` → `EmailSourceData`; `build_evidence_anchors` derives the
anchors; the UI fetches the analysis → `email_source.evidence_anchors`; the
preview fetches the stored bytes via the download endpoint → the text layer
contains `document_text` → `resolveAnchorStatus` returns **located**.

## 4. Security / isolation

Sandbox-only (double-gated, same as the other `/_sandbox/*` routes); tenant-scoped
reads/writes; never mounted when `ASOE_ENV != sandbox`. No audit-chain
contamination — fixture wiring only. `/_sandbox/tenant/reset` clears seeded state.

## 5. Tests (write first)

In `tests/test_sandbox_routes.py` (run with `ASOE_ENV=sandbox`):

1. `test_seed_creates_located_analysis` — POST seed with `document_text`
   containing the anchor texts → GET the analysis for `exception_id` →
   `email_source` present; `attachment_manifest[0]` has `attachment_id` + `sha256`;
   `evidence_anchors` non-empty; every `anchor.text` is a substring of
   `document_text` (so the UI will locate it).
2. `test_seeded_bytes_contain_document_text` — GET
   `/cases/{case_id}/attachments/{attachment_id}` → for PDF the body starts
   `%PDF` and contains `document_text`; for text/csv the body equals it.
3. `test_seed_requires_sandbox_env` — with `ASOE_ENV` ≠ `sandbox` → `403`.
4. `test_seed_is_tenant_scoped` — the attachment + record live under the caller's
   tenant; a cross-tenant `get_attachment` returns `None`.

## 6. Acceptance

The 4 journeys in `tests/browser/attachment-evidence.spec.ts` seed via this
endpoint and pass (located / unlocated / ambiguous / banner); the §5 pytest is
green. (Governance — retention, encryption, audit-chain routing — is explicitly
out of scope: preprod fixture.)
