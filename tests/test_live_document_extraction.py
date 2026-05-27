"""PARITY-6.2 — Live AzureDI backend for document_extraction.

The ``DocumentExtractionGateway`` already has a backend seam
(``propose(source_sha256, hint) -> dict``). PARITY-6.2 wires
``LiveDocumentIntelligenceBackend`` behind the same seam, gated on
``ASOE_DOCUMENT_EXTRACTION_BACKEND=live``. Per ML review, every
extraction result records a drift sample via
``api.metrics.record_extraction_drift`` so the
``extraction-drift`` alert (from PARITY-7) has data to evaluate.

Live transport runs only against recorded fixtures on the red-green
path; nightly ``-m live`` mark hits real Azure.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "fallback")


@pytest.fixture
def _clean_extraction_metrics():
    from api import metrics
    metrics.reset_extraction_metrics()
    yield
    metrics.reset_extraction_metrics()


class TestBackendSelector:
    def test_default_backend_is_recorded(self, monkeypatch):
        """Without ASOE_DOCUMENT_EXTRACTION_BACKEND, the selector keeps
        the recorded backend — reversible-by-env per the parity plan."""
        monkeypatch.delenv("ASOE_DOCUMENT_EXTRACTION_BACKEND", raising=False)
        from gateways.document_extraction import resolve_backend, RecordedDocumentExtractionBackend
        backend = resolve_backend()
        assert isinstance(backend, RecordedDocumentExtractionBackend)

    def test_live_backend_when_env_says_live(self, monkeypatch):
        monkeypatch.setenv("ASOE_DOCUMENT_EXTRACTION_BACKEND", "live")
        # Live backend needs the AzureDI endpoint env vars at construction
        # time so a misconfigured deploy fails loud rather than silently
        # serving recorded data.
        monkeypatch.setenv(
            "ASOE_DOCUMENT_INTELLIGENCE_ENDPOINT",
            "https://stub-di.example/",
        )
        monkeypatch.setenv("ASOE_DOCUMENT_INTELLIGENCE_KEY", "stub-key")
        from gateways.document_extraction import resolve_backend, LiveDocumentIntelligenceBackend
        backend = resolve_backend()
        assert isinstance(backend, LiveDocumentIntelligenceBackend)

    def test_live_backend_missing_creds_fails_loud(self, monkeypatch):
        monkeypatch.setenv("ASOE_DOCUMENT_EXTRACTION_BACKEND", "live")
        monkeypatch.delenv("ASOE_DOCUMENT_INTELLIGENCE_ENDPOINT", raising=False)
        monkeypatch.delenv("ASOE_DOCUMENT_INTELLIGENCE_KEY", raising=False)
        from gateways.document_extraction import resolve_backend
        with pytest.raises(RuntimeError):
            resolve_backend()


class TestDriftMetricRecording:
    def test_extract_anchors_records_drift_sample(
        self, monkeypatch, _clean_extraction_metrics,
    ):
        """Every extraction (recorded or live) records the per-field
        confidence + containment into the drift series so the
        ``extraction-drift`` evaluator has samples."""
        from gateways.document_extraction import (
            DocumentExtractionGateway,
            RecordedDocumentExtractionBackend,
        )
        from api import metrics

        # Build a hand-rolled recording in a temp dir so the test is
        # independent of repo fixtures.
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            rec_dir = Path(td)
            recording = {
                "case": "TEST-CASE-1",
                "model_id": "prebuilt-invoice-test",
                "prompt_hash": "ph-1",
                "candidates": [
                    {"candidate_id": 1, "page": 1, "bbox": [0.0, 0.0, 1.0, 1.0],
                     "text": "Acme Corp"},
                ],
                "fields": [
                    {
                        "chosen_id": 1, "text": "Acme Corp",
                        "supports_ref": "vendor_name",
                        "label": "Vendor",
                        "confidence": 0.92,
                    },
                ],
            }
            (rec_dir / "TEST-CASE-1.recorded.json").write_text(json.dumps(recording))

            backend = RecordedDocumentExtractionBackend(recordings_dir=str(rec_dir))
            gw = DocumentExtractionGateway(backend=backend)
            anchors = gw.extract_anchors(
                attachment_id="att-1",
                source_sha256="sha-1",
                hint={"case": "TEST-CASE-1"},
            )
            assert len(anchors) == 1

        snapshot = metrics.snapshot_extraction_metrics()
        # A drift sample was recorded for the (model_id, prompt_hash) tuple.
        assert ("prebuilt-invoice-test", "ph-1") in snapshot["contain"], (
            "extract_anchors must call record_extraction_drift so the "
            "extraction-drift alert has data — currently silent"
        )


class TestLiveBackendLiveTransport:
    def test_live_backend_execute_raises_off_live_mark(self, monkeypatch):
        """The live HTTP call to AzureDI is gated to nightly ``-m live``.
        Calling propose on the recorded path must NOT actually hit
        AzureDI — the red-green path must remain deterministic."""
        monkeypatch.setenv("ASOE_DOCUMENT_EXTRACTION_BACKEND", "live")
        monkeypatch.setenv(
            "ASOE_DOCUMENT_INTELLIGENCE_ENDPOINT",
            "https://stub-di.example/",
        )
        monkeypatch.setenv("ASOE_DOCUMENT_INTELLIGENCE_KEY", "stub-key")
        from gateways.document_extraction import LiveDocumentIntelligenceBackend
        backend = LiveDocumentIntelligenceBackend()
        # supports() declares the live-backend surface, model_id reports
        # the pinned id (Decision Q2 — prebuilt-invoice default).
        assert backend.model_id  # non-empty
        # propose() raises rather than making a real HTTP call.
        with pytest.raises(NotImplementedError):
            backend.propose(source_sha256="x", hint={"case": "TEST"})
