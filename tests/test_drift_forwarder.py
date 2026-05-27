"""PARITY-7 followup — drift-alert App Insights forwarder.

The drift detector at ``api.observability.drift_alert_integration``
ships but isn't called by a scheduled context. This forwarder
(``scripts.run_drift_forwarder``) is the Container Apps Job glue —
on every cron tick it calls ``evaluate_all_extraction_drift()``,
filters for ``drift_detected=True``, and emits a structured
``extraction-drift`` log event each time. The logger is configured
to flow through the OTel exporter when
``APPLICATIONINSIGHTS_CONNECTION_STRING`` is set; locally the events
land on stdout for ``az containerapp job logs show``.
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "fallback")


@pytest.fixture
def _clean_metrics():
    from api import metrics
    metrics.reset_extraction_metrics()
    yield
    metrics.reset_extraction_metrics()


class TestForwarderNoSamples:
    def test_no_samples_logs_zero_count_and_exits_zero(
        self, monkeypatch, caplog, _clean_metrics,
    ):
        from scripts.run_drift_forwarder import main
        with caplog.at_level(logging.INFO):
            exit_code = main()
        assert exit_code == 0
        joined = " ".join(r.message for r in caplog.records)
        assert "drift_forwarder_done" in joined
        assert "drift_count=0" in joined


class TestForwarderFiresOnDrift:
    def test_drift_detected_emits_alert_event(
        self, monkeypatch, caplog, _clean_metrics,
    ):
        # Seed the metric collector with samples that cross the 5pp
        # median-drop threshold the drift detector uses.
        from api import metrics
        # 200 baseline samples around 0.92 containment, 50 recent
        # around 0.82 — well past the 5pp threshold.
        for _ in range(200):
            metrics.record_extraction_drift(
                model_id="m1", prompt_hash="ph1",
                confidence=0.92, containment=0.92,
            )
        for _ in range(50):
            metrics.record_extraction_drift(
                model_id="m1", prompt_hash="ph1",
                confidence=0.82, containment=0.82,
            )

        from scripts.run_drift_forwarder import main
        with caplog.at_level(logging.INFO):
            exit_code = main()
        assert exit_code == 0
        # The drift event was logged with the canonical alert name so
        # App Insights / Log Analytics queries find it.
        messages = " ".join(r.message for r in caplog.records)
        assert "extraction-drift" in messages
        assert "drift_forwarder_done" in messages

    def test_no_drift_when_containment_stable(
        self, monkeypatch, caplog, _clean_metrics,
    ):
        from api import metrics
        # All samples cluster around 0.92 — no drift.
        for _ in range(250):
            metrics.record_extraction_drift(
                model_id="m2", prompt_hash="ph2",
                confidence=0.92, containment=0.92,
            )
        from scripts.run_drift_forwarder import main
        with caplog.at_level(logging.INFO):
            exit_code = main()
        assert exit_code == 0
        msg = " ".join(r.message for r in caplog.records)
        assert "drift_count=0" in msg


class TestForwarderResilience:
    def test_evaluator_exception_does_not_crash(
        self, monkeypatch, caplog,
    ):
        # If the evaluator raises, the forwarder logs and exits 1 so
        # Container Apps surfaces the failure to the operator — never
        # silently masks a problem with the drift signal.
        from scripts import run_drift_forwarder as mod
        monkeypatch.setattr(
            mod, "_evaluate_all",
            lambda: (_ for _ in ()).throw(RuntimeError("metric snapshot failed")),
        )
        with caplog.at_level(logging.ERROR):
            exit_code = mod.main()
        assert exit_code == 1
        assert any(
            "drift_forwarder_error" in r.message for r in caplog.records
        )
