# Attachment Preview & Evidence Highlighting — Rollout & Implementation Plan

**Document type:** Operational planning doc (not an ADR — references ADR-043 / ADR-044 / ADR-045 for binding decisions).
**Status:** Active
**Date:** 2026-05-25
**Owners:** Principal AI/Agentic Engineering Architect; Frontend Platform; Data Engineering; ML/Eval; Compliance (veto); Security; Product Owner.
**Provenance:** 11-expert independent review (2026-05-25) → joint convergence (Round 3). PO decisions: highlight **all** evidence fields by default; **plan Phase 2 now**; **split into ADR-043/044/045**.
**Extends:** `docs/test-strategy/customer-inbox-tdd-strategy.md` (the ratified TDD/BDD strategy — *the test suite is the spec; nothing ships until its red test exists*).

This plan turns the converged decisions into a test-first execution sequence. It
does **not** restate the ADRs — it sequences the work, maps every item to a
failing test in the ratified pyramid, and names the human/compliance sign-off
gates.

---

## 1. Converged decisions (the joint-session output)

| # | Decision | ADR |
|---|---|---|
| D1 | Phase 1 = sandboxed preview + highlight **all** text-derived evidence anchors | 043 |
| D2 | **Safety bar:** per-anchor `LOCATED/UNLOCATED/AMBIGUOUS`; UNLOCATED shown as loudly as a hit; runtime verifier (rendered text under box == anchor text); audit-authoritative unit = `(attachment_id, sha256, text, supports_ref)`, decoupled from pixels | 043 |
| D3 | Backend supplies a deterministic `MatchKey`; UI does a pure literal locate (projector stays dumb) | 043 |
| D4 | One `EvidenceAnchor` + `anchor_source` discriminator + `source_sha256` + **closed, parity-locked `supports_*` vocabulary**; on the OpenAPI→TS gate day one; geometry tripwire-locked off in Phase 1 | 043 |
| D5 | Security as tested invariants: exact sandbox tokens (never `allow-same-origin`+`allow-scripts`); CSP `default-src 'none'` + **`connect-src 'none'`** + `object-src 'none'`; deny SVG; magic-byte default-deny; PDF.js eval/JS-actions off + pin/SRI + SCA; IDOR test on preview | 043 |
| D6 | `case_id` explicit prop (no context); bytes via `api.ts` (RBAC); anchors via `analysis.*` | 043 |
| D7 | Observability = Phase-1 DoD: `highlight_outcome{located\|unlocated\|ambiguous}`, match-hit-ratio SLI, zero-highlight alert, preview-latency SLO; bounded cardinality | 043 |
| D8 | Storage-portability contract test lands in Phase 1; object storage + metadata/URI + signed URLs = GA precondition | 043→044 |
| D9 | Phase 2 = candidate proposer (select real OCR boxes, never generate); runtime verifier spine; geometry `required_for_audit=False` → degrade to text; idempotency `(sha256, model_id)`; circuit-breaker + outbox parity; async | 045 |
| D10 | Eval = containment + page-accuracy (zero-tolerance wrong page) + confidence ECE; IoU diagnostic-only; golden set bootstrapped via text-layer weak labels + hand-label scanned; xfail-strict + CODEOWNERS | 045 |
| D11 | Spatial anchors bind to a frozen, hashed page rendition (raster/pdf + dpi + renderer_version); coordinate-validity test | 044/045 |
| D12 | Decision-quality gate: wire `highlight_shown` into the #11 automation-bias SLIs; non-dismissable "highlight ≠ authorization" banner; pre-GA A/B with seeded wrong/missing highlights | 043 |

## 2. Execution phases & checkpoints

Each checkpoint is a stop: red→green summary + (where marked) human/compliance
sign-off before proceeding. Push once per checkpoint when green; open the PR as a
draft.

| CP | Goal | Exit |
|---|---|---|
| **CP-A** | ADRs authored (043 revised, 044, 045) + this plan | *this commit* — review |
| **CP-B** | Phase-1 **failing tests** written (red / xfail-strict) — §3 backlog | red suite reviewed; no impl yet |
| **CP-C** | Backend to green: `EvidenceAnchor` contract + closed vocab + `MatchKey` + composer projection + OpenAPI→TS regen + metrics | `pytest tests/` green; contract gate green |
| **CP-D** | UI to green: `AttachmentPreview` (sandbox/CSP), literal-locate + safety bar, `case_id` wiring, banner, download button | vitest + Playwright + axe green; **security review sign-off** on inline render |
| **CP-E** | ADR-044 storage swap (object store + signed URLs + erasure) — GA precondition | portability/erasure tests green; **compliance sign-off** on retention/erasure |
| **CP-F** | ADR-045 spatial: eval task + `document_extraction` gateway + verifier + async pipeline | eval gate met; **compliance sign-off** on new provenance; provider decision ratified |

CP-A→CP-D deliver the shippable Phase 1. CP-E is the GA gate. CP-F is Phase 2.

## 3. Test-first backlog (red before code; mapped to the ratified pyramid)

Homes/bands per `customer-inbox-tdd-strategy.md` §2. `xfail-strict` per §3 for
gates ahead of impl.

**Deterministic base — asoe2 (CP-B/CP-C)**
* `tests/test_analysis_composer.py` — composer projects an `EvidenceAnchor` per extracted field from a frozen `state.enrichment_context`; `anchor_source == "text_derived"`; **`page`/`bbox`/`confidence` are None**; `source_sha256` set; correct `supports_kind`/`supports_ref`/`label`.
* `tests/test_evidence_anchor_vocabulary.py` — `supports_kind`/`supports_ref` come from the closed enum/registry; unknown ref rejected.
* `tests/test_match_key.py` — `MatchKey` (`normalized_text`, `occurrence_index`) is deterministic; repeated-token doc yields the correct occurrence index (the AMBIGUOUS source).
* `tests/test_audit_registry_coverage.py` (extend) — `EvidenceAnchor` registered; audit tuple = `(attachment_id, sha256, text, supports_ref)`, excludes position.

**Contract / parity (CP-C)**
* `tests/test_evidence_anchor_openapi_contract.py` — regenerated `openapi/asoe2.openapi.json` carries `EvidenceAnchor` (spatial fields nullable); `npm run generate-types` + `verify-types` green.
* asoe-ui `tests/architectural/supports_ref_vocab_parity.test.ts` — TS `supports_*` consts match the Python vocabulary (the value-level drift `generated.ts` can't catch).

**Security abuse-cases (CP-B/CP-D)** — extend DoR #10 (`email-render-xss.spec.ts`, `tests/test_security.py`)
* iframe `sandbox` asserts exactly `allow-scripts`, **never** `allow-same-origin`.
* CSP asserts `default-src 'none'`, `connect-src 'none'`, `object-src 'none'`, no `unsafe-*`.
* magic-byte corpus: PDF-with-JS, SVG-as-png, GIFAR/polyglot, HTML-as-text, oversize → renderer selection by magic bytes, scripting disabled, SVG denied, oversize rejected.
* **IDOR**: tenant-A operator + tenant-B / wrong-`case_id` `attachment_id` → 404 on the preview path.
* XSS payload in filename + rendered text layer → no execution.

**Tripwires (narrow — one per invariant; CP-B)**
* "no geometry in Phase 1" — Phase-1 code never sets `page`/`bbox`/`confidence`.
* `EmailSourceSection` receives `caseId` and never imports `fetch`/`exceptionsApi` directly (bytes only via `api.ts`).
* non-dismissable banner present (fix-encoded predicate a behavioural test can't reach).

**Frontend base + journeys (CP-D)**
* component: `AttachmentPreview` renders pdf/img/text/csv; jsdom canvas mock for PDF.js; axe sweep.
* BDD `tests/browser/operator-journeys/`:
  * `email-entry-evidence-verify.spec.ts` — open attachment → all field anchors highlighted + labelled.
  * `evidence-anchor-unlocated.spec.ts` — anchor `text` not in text layer → **UNLOCATED state shown loudly**, download still available, **no silent/empty highlight**.
  * `evidence-anchor-ambiguous.spec.ts` — repeated token → AMBIGUOUS ("position approximate"), not a silent guess.
  * `highlight-not-authorization.spec.ts` — banner present; Layer-2/source open recorded on authorise.

**Observability-as-contract (CP-C/CP-D)**
* `tests/test_metrics_endpoint.py` (extend) — `/metrics` exposes `highlight_outcome_total{result,mime}`, `preview_render_total`, preview-latency histogram with **bounded labels** (fails if `attachment_id` is added); zero-highlight condition emits.
* `tests/test_reviewer_override_sli.py` (extend, #11) — `highlight_shown` is a recorded dimension on override/Layer-2-open/dwell.

**Storage (CP-B lands red; CP-E green) — ADR-044**
* `tests/test_attachment_store_portability.py` — same suite vs in-memory + object-store backend (`put/get/list/clear` parity, content-stripped list, sha256 integrity).
* `tests/test_attachment_signed_url.py` — short-TTL, tenant+case-scoped, unusable after expiry / cross-tenant.
* `tests/test_attachment_erasure_cascade.py` — erase → bytes + renditions + anchors gone, **audit chain + tombstone preserved**.
* tripwire — no new BYTEA blob columns after V016.

**Eval — ADR-045 (CP-B `xfail-strict`; CP-F green)**
* `tests/eval/datasets/extraction_spatial/*.jsonl` — rows `{id,input,expected:{page,bbox,text},labeler,model_pinned}`.
* `tests/eval/test_spatial_extraction.py` — **containment** ≥ threshold (PR gate, replay); **page-accuracy == 1.0**; coordinate-hallucination reported; **anchor confidence ECE** ≤ target.
* `tests/test_gateways.py` (extend) — `document_extraction` breaker OPEN / timeout → composer still emits Phase-1 text anchors (**degrade-to-text**); replay → byte-identical anchors.
* `tests/test_document_extraction_verifier.py` — deliberately displaced candidate → verifier drops geometry → text anchor (no confident wrong box).
* outbox idempotency — same `(sha256, model_id)` → one effect row; malformed result → DLQ, not the anchor set.

## 4. Risk register (top, from the panel)

| Risk | Mitigation (decision) | Owner |
|---|---|---|
| Confident-wrong / silently-missing highlight on a SOX surface | Safety bar D2 + runtime verifier; decision-quality A/B D12 | UX / ML |
| Inline-render XSS **and data exfiltration** | D5 invariants incl. `connect-src 'none'`; CP-D security sign-off | Security |
| Preview makes blobs a hot read path on the financial-write DB | D8 / ADR-044 object storage before GA | Data Eng / SRE |
| Hallucinated coordinates (shape-valid, semantically wrong) | D9 select-not-generate + D10 containment gate + verifier | ML / Agentic |
| Automation bias amplified by all-field highlighting | D12 SLIs + banner + A/B | UX / PO |
| Audit can't distinguish text vs spatial provenance | `anchor_source` discriminator (D4) + frozen rendition (D11) | Compliance / Data Eng |
| PDF.js CVE / bundle drift | pin + SRI + continuous SCA gate (D5) | Frontend Platform |

## 5. Sign-off gates (cannot be satisfied silently)

* **Security review** — inline-render surface, before CP-D ships (D5 tests are the checklist).
* **Compliance CODEOWNERS** — `audit_bearing_registry.yaml` (`EvidenceAnchor`, erasure tombstone) and `tests/eval/thresholds.yaml`; retention/erasure (CP-E); new spatial provenance (CP-F).
* **Product Owner** — decision-quality A/B catch-rate (D12) before GA of highlighting.
* **Autonomous loop contract** (strategy §9) — write red before green; keep red-green off live models; stop for human + compliance sign-off on the audit chain, thresholds, and any newly audit-bearing provenance; checkpoint at each CP with a red/green summary.

---

*This plan extends the ratified strategy; the backlog items in §3 are to be folded into `customer-inbox-tdd-strategy.md` (its §6/§7 style) at CP-B when the red gates are committed.*
