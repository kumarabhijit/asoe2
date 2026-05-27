# `document_extraction` connector — CHANGELOG

Schema-drift attribution trail for the Azure Document Intelligence live
backend (`gateways/document_extraction.py::LiveDocumentIntelligenceBackend`).
One row per fixture refresh / contract change per
`docs/ops/fixture-capture.md` cadence.

Format: `YYYY-MM-DD | operation(s) | captured-by | reason`

---

## 2026-05-27 — initial live backend + drift wiring

`PARITY-6.2` — `LiveDocumentIntelligenceBackend` lands behind
`ASOE_DOCUMENT_EXTRACTION_BACKEND=live`. Default `recorded` keeps the
red-green path on the existing `RecordedDocumentExtractionBackend`.
`resolve_backend()` is the single selector both runtime + tests use.

Constructor reads `ASOE_DOCUMENT_INTELLIGENCE_ENDPOINT` +
`ASOE_DOCUMENT_INTELLIGENCE_KEY` (or
`ASOE_DOCUMENT_INTELLIGENCE_USE_MI=1` for Managed Identity); refuses
to construct without either to make a misconfigured deploy loud.

Every `DocumentExtractionGateway.extract_anchors` call now feeds the
drift series via `api.metrics.record_extraction_drift` so the
`extraction-drift` alert in `api.observability.drift_alert_integration`
has data even on the recorded path. Containment proxy = verified-
spatial-anchor rate (anchors that survive `verify_anchor_geometry`).

Live HTTP transport (`propose()`) is gated to the nightly `-m live`
mark — it raises `NotImplementedError` on the red-green path so an
accidental live call surfaces loudly.

No fixtures refreshed yet — the existing
`tests/eval/datasets/extraction_spatial/seed.jsonl` (12 rows across
born_digital/scanned/multi_page/table_heavy from PARITY-7) is the
current baseline; nightly drift comparison starts when the first live
`-m live` job runs against the AzureDI account.
