# Session prompt — finish ADR-043 / ADR-044 / ADR-045

**Document type:** Operational hand-off prompt (not an ADR).
**Status:** Active
**Date:** 2026-05-25
**Use:** paste the fenced block below into a fresh agent session to finish all
pending work for the three attachment-preview / evidence-highlighting ADRs and
move each to *Accepted*.

**Standing decisions baked in (PO):**
- Security sign-off for the inline-render surface is **granted** (preprod; PO holds
  the veto) — the session must not block on it.
- **Data governance is out of scope** (preprod, far from GA) — retention/TTL,
  encryption-at-rest, audit-chain tombstone routing, PII policy, and
  compliance/CODEOWNERS sign-offs are explicitly not built.
- Priority: finish the **critical path (ADR-043)** first, then the **essential**
  work (ADR-044 non-governance + ADR-045 Phase 2).

Companion specs the prompt references: `docs/specs/sandbox-attachment-anchor-seed.md`,
`docs/plans/attachment-preview-evidence-rollout.md`,
`docs/test-strategy/customer-inbox-tdd-strategy.md` (§10).

---

```
You are continuing a feature spanning THREE ADRs authored this session, all to be
finished and moved to Accepted:
  - ADR-043  attachment preview + in-document evidence highlighting (Phase 1)
  - ADR-044  document storage & lifecycle
  - ADR-045  spatial evidence extraction (Phase 2, document-AI candidate proposer)
Read all three in asoe2/docs/adr/, plus docs/plans/attachment-preview-evidence-rollout.md,
docs/test-strategy/customer-inbox-tdd-strategy.md (§10), and
docs/specs/sandbox-attachment-anchor-seed.md.

Repos & branch: asoe2 (Python/FastAPI/LangGraph) and asoe-ui (Next.js/TS, consumes
asoe2 OpenAPI). Work on `claude/nice-bardeen-k2uch` in BOTH; FIRST sync each to
origin/main (git fetch origin main && git merge --ff-only origin/main). Open PRs:
asoe2 #173 (seed spec), asoe-ui #191 (download/coverage) — keep them updated or open
new ones. Push in small reviewable commits.

AUTHORISATIONS / SCOPE (do NOT stop to ask):
  - Security sign-off for the inline-render surface is GRANTED (preprod; I hold the
    veto). Keep the security behaviours already in code (magic-byte allowlist, SVG/HTML
    deny, PDF.js no-scripting) but do not wait on any review gate.
  - DATA GOVERNANCE IS OUT OF SCOPE (preprod, far from GA). Do NOT build: retention/TTL,
    encryption-at-rest, routing erasure tombstones into the audit chain, PII policy, or
    compliance/CODEOWNERS sign-off gates.
  - Keep red-green tests OFF live models/providers (RecordedGatewayBackend + recorded
    fixtures); any live provider call goes in a nightly/-m live path only.

DISCIPLINE: strict test-first (failing test, then implement); dumb-projector UI
(Guardrail #6 — render backend-authoritative analysis.* only; bytes only via
src/lib/api.ts); TS types mirror backend; never weaken a gate to go green; commit
small. When a browser/tool is needed, install it (e.g. npx playwright install
--with-deps chromium) rather than skipping.

ALREADY DONE (green; do not redo — see the ADRs/docs for detail):
  asoe2: EvidenceAnchor/MatchKey/EvidenceSupportsKind, build_evidence_anchors +
    compute_match_keys, detect_preview_format, preview/highlight metrics, EvidenceAnchor
    registry rows, ObjectStoreBackend + erase_attachment + get_erasure_tombstone +
    per-backend delete, document_extraction.py (select_candidate_box,
    verify_anchor_geometry, build_spatial_anchor), tests/eval/spatial_scorer.py. Full
    pytest green.
  asoe-ui: AttachmentPreview + AttachmentDownloadButton + EmailSourceSection wiring
    (caseId threaded), previewFormat/evidenceAnchor libs, attachmentsApi.getBlob with
    type-correct mock bytes that embed evidence text (mock-data/attachment-bytes.ts),
    inbox mock coverage (email_source + evidence_anchors on all inbox cases). tsc +
    vitest + build green. 4 Playwright journeys exist as test.fixme.

=== PRIORITY 1 — CRITICAL PATH: finish ADR-043 (preview end-to-end + verified) ===
P1.1  Implement the sandbox seed endpoint EXACTLY per
      docs/specs/sandbox-attachment-anchor-seed.md:
      POST /api/v1/_sandbox/seed/email-attachment-anchors (in api/routes/sandbox.py),
      tests-first in tests/test_sandbox_routes.py. It creates an EMAIL_ENTRY case with a
      stored attachment whose BYTES contain the document text + email_source_context +
      extracted_entities, so the composer derives located anchors.
P1.2  Enable the 4 Playwright journeys (remove .fixme in
      tests/browser/attachment-evidence.spec.ts), reconcile request keys with the
      endpoint (snake_case), install Chromium, run `npm run test:browser` against the
      seeded backend; fix until green. This verifies real PDF.js render + highlighting.
P1.3  Wire the decision-quality signal (ADR-043 D12): emit a `highlight_shown`
      dimension into the automation-bias SLIs (api/metrics.py reviewer-activity + the
      asoe-ui report path) so it's measurable. (A/B study is manual — leave a note.)
P1.4  Move ADR-043 -> Accepted (ships on in-DB storage in preprod).

=== PRIORITY 2 — ESSENTIAL: finish ADR-044 (non-governance) + ADR-045 (Phase 2) ===
ADR-044 (storage; governance items above are OUT — skip them):
P2.1  Add a working ObjectStore blob driver behind the existing _BlobStore seam — a
      local filesystem/in-process driver usable in tests without cloud creds; put a real
      S3/GCS/MinIO driver behind an env flag (live path only). Wire env-driven backend
      selection. Reuse the existing storage-portability contract test.
P2.2  Add a scoped, short-TTL read path (signed-URL or streamed) for attachment bytes,
      with expiry + cross-tenant tests. (No retention/encryption — governance, skip.)
P2.3  Implement AttachmentRepository.delete so erase_attachment works on the DB backend.
P2.4  Frozen renditions: bind a spatial bbox to a hashed page render (raster/pdf + dpi +
      renderer version) — minimal, just enough for coordinate validity. (Needed by P2.6.)
P2.5  Move ADR-044 -> Accepted for the non-governance scope (note governance deferred).
ADR-045 (spatial extraction, end-to-end, off live models):
P2.6  DocumentExtractionGateway producing an OCR candidate set
      {candidate_id,page,bbox,text} and using build_spatial_anchor (select-not-generate +
      verify_anchor_geometry). Real provider (managed OCR or self-hosted docTR) behind the
      seam on a live/nightly path; default = RecordedGatewayBackend replay. Circuit-breaker
      parity. Async/idempotent via the effect outbox keyed on (sha256, model_id).
P2.7  Recorded fixtures (tests/fixtures/gateway/document_extraction/*.json) + golden
      dataset (tests/eval/datasets/extraction_spatial/*.jsonl); wire
      tests/eval/spatial_scorer.py (containment + page-accuracy + confidence ECE) into the
      eval harness with thresholds in tests/eval/thresholds.yaml (NO CODEOWNERS gate).
P2.8  Composer wiring: populate SPATIAL anchors (bound to a frozen rendition) when geometry
      is available, degrading to text anchors on miss/outage (required_for_audit=False).
      Add EvidenceAnchor spatial registry rows (no compliance gate).
P2.9  Per-page cost guardrail in contracts/policy.py + cost meter + drift signal in
      api/metrics.py.
P2.10 Frontend: render spatial bbox overlays on the PDF.js canvas when page+bbox present;
      keep the text-derived safety bar as the authoritative fallback. Component test.
P2.11 Move ADR-045 -> Accepted once the eval gate passes on the golden set.

OUT OF SCOPE (do not build): all data-governance items (retention/TTL, encryption,
audit-chain tombstone routing, PII policy, compliance/CODEOWNERS sign-offs).

VERIFY before each push:
  asoe2:   python -m pytest -q  (+ targeted new tests)
  asoe-ui: npm run typecheck && npx vitest run && npm run build && npm run verify-types
  journeys: npx playwright install --with-deps chromium && npm run test:browser
DONE = seed endpoint live; the 4 journeys pass in a browser; PDF preview opens + shows
located highlights (mock + seeded-real); spatial overlays render when geometry exists and
the spatial eval gate passes on the golden set; object-store driver + scoped read + DB
delete + frozen renditions in place; full suites green; ADR-043/044/045 all marked
Accepted; branches pushed and PRs updated.
```
