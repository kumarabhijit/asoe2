"""PARITY-7 — wire the extraction-drift alert into the metric collector.

The drift detector in ``api.observability.drift_alert`` is pure; the
metric collector in ``api.metrics`` already records per-extraction
containment samples. The bridge here pulls the recent samples
(7-day approximation: last N) and the baseline (N before that) from
the collector and feeds them into the detector.

Contract under test:

  * ``evaluate_extraction_drift_for_model(model_id, prompt_hash)``
    returns a ``DriftVerdict`` for that model. No samples → no alert.
  * ``evaluate_all_extraction_drift()`` iterates every
    (model_id, prompt_hash) key the collector knows about and
    returns the list of verdicts (callers filter by
    ``drift_detected=True`` to forward to App Insights).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_extraction(monkeypatch):
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "fallback")
    from api import metrics
    metrics.reset_extraction_metrics()
    yield
    metrics.reset_extraction_metrics()


class TestDriftIntegration:
    def test_no_samples_returns_no_drift(self):
        from api.observability.drift_alert_integration import (
            evaluate_extraction_drift_for_model,
        )

        v = evaluate_extraction_drift_for_model(
            model_id="prebuilt-invoice", prompt_hash="azuredi-prompt-v0",
        )
        assert v.drift_detected is False

    def test_drift_detected_when_recent_drops(self):
        from api import metrics
        from api.observability.drift_alert_integration import (
            evaluate_extraction_drift_for_model,
        )

        # Baseline (older samples): healthy containment 0.92-0.93.
        for c in (0.92, 0.93, 0.91, 0.92, 0.93, 0.92, 0.91):
            metrics.record_extraction_drift(
                model_id="prebuilt-invoice",
                prompt_hash="azuredi-prompt-v0",
                confidence=0.95,
                containment=c,
            )
        # Recent samples: dropped to 0.83-0.84.
        for c in (0.83, 0.84, 0.82):
            metrics.record_extraction_drift(
                model_id="prebuilt-invoice",
                prompt_hash="azuredi-prompt-v0",
                confidence=0.95,
                containment=c,
            )

        v = evaluate_extraction_drift_for_model(
            model_id="prebuilt-invoice",
            prompt_hash="azuredi-prompt-v0",
            recent_n=3,
            baseline_n=7,
        )
        assert v.drift_detected is True
        assert v.median_drop_pp >= 5.0

    def test_evaluate_all_iterates_keys(self):
        from api import metrics
        from api.observability.drift_alert_integration import (
            evaluate_all_extraction_drift,
        )

        for m, h in (("a", "h1"), ("b", "h2")):
            for c in (0.93, 0.92, 0.93, 0.92, 0.93, 0.92, 0.93, 0.92):
                metrics.record_extraction_drift(
                    model_id=m, prompt_hash=h,
                    confidence=0.9, containment=c,
                )

        results = evaluate_all_extraction_drift(recent_n=2, baseline_n=6)
        keys = {(v.model_id, v.prompt_hash) for v in results}
        assert ("a", "h1") in keys
        assert ("b", "h2") in keys
