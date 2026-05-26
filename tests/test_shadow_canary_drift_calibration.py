"""PARITY-6/7 advanced plumbing tests.

Covers the runtime gates that the parity plan calls out but which
were left as scaffolding only in the first pass:

  * **Shadow-mode runner**: a wrapper that calls a real-and-stub
    pair in parallel, diffs the outputs against the Q9 thresholds
    (audit-bearing 0%, derived ≤5%), and writes a compliance-
    reviewable diff log. The connector flip from shadow → real-only
    happens once 7 consecutive days post a < 1% audit-bearing diff
    rate.
  * **Canary traffic-split**: percentage-based gateway rollout
    (5% → 25% → 100%) keyed by a deterministic hash of the case_id
    so the same case sees the same path every time.
  * **Drift alert**: `extraction-drift` fires when the 7-day rolling
    median containment drops > 5pp.
  * **Brier-score calibration**: Phase 7 confidence gate — Brier
    score over the golden set must be ≤ 0.1; otherwise recalibrate
    or replace AzureDI confidence.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _quiet_lifespan(monkeypatch):
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "fallback")


class TestShadowModeRunner:
    def test_runner_executes_both_real_and_stub(self):
        from gateways.shadow_mode import ShadowRunner

        def real_call(**kw):
            return {"foo": "bar", "n": 1}

        def stub_call(**kw):
            return {"foo": "bar", "n": 1}

        runner = ShadowRunner(connector="test_gw", operation="echo")
        out = runner.run(real=real_call, stub=stub_call, params={})
        # Real is the authoritative return; stub is shadow-only.
        assert out == {"foo": "bar", "n": 1}

    def test_runner_audit_bearing_diff_recorded(self):
        from gateways.shadow_mode import ShadowRunner, get_diff_log

        get_diff_log().clear()

        def real_call(**kw):
            return {"po_number": "PO-AAA", "ship_to": "Atlanta"}

        def stub_call(**kw):
            return {"po_number": "PO-BBB", "ship_to": "Atlanta"}

        runner = ShadowRunner(
            connector="sap_order",
            operation="validate",
            audit_bearing_fields={"po_number"},
            derived_fields={"ship_to"},
        )
        runner.run(real=real_call, stub=stub_call, params={})

        entries = get_diff_log()
        assert len(entries) == 1
        assert entries[0].connector == "sap_order"
        assert entries[0].audit_bearing_diffs == {"po_number"}
        assert entries[0].derived_diffs == set()

    def test_runner_distinguishes_none_from_missing(self):
        """Regression for code-review REAL BUG: dict.get collapses
        ``key: None`` and missing-key to None, so a real-vs-stub
        presence diff would silently register as "no diff" if the
        diff used ``.get`` alone. The Q9 acceptance gate would then
        pass with the audit-bearing variance silently hidden."""
        from gateways.shadow_mode import ShadowRunner, get_diff_log
        get_diff_log().clear()

        def real_call(**kw):
            return {"po_number": "PO-123", "status": None}

        def stub_call(**kw):
            return {"po_number": "PO-123"}  # status MISSING, not None

        runner = ShadowRunner(
            connector="sap_order",
            operation="validate",
            audit_bearing_fields={"po_number", "status"},
        )
        runner.run(real=real_call, stub=stub_call, params={})

        entries = get_diff_log()
        assert "status" in entries[0].audit_bearing_diffs, (
            "presence-of-key diff was silently dropped — Q9 audit log "
            "would miss a real-vs-stub variance"
        )

    def test_runner_real_failure_does_not_crash(self):
        """Shadow mode is opt-in observability; a real-side failure
        falls back to the stub return so the request still completes
        (the diff log records the failure for review)."""
        from gateways.shadow_mode import ShadowRunner, get_diff_log
        get_diff_log().clear()

        def real_call(**kw):
            raise RuntimeError("upstream timeout")

        def stub_call(**kw):
            return {"po_number": "PO-X"}

        runner = ShadowRunner(connector="sap_order", operation="validate")
        out = runner.run(real=real_call, stub=stub_call, params={})
        # Falls back to the stub return on real-side error.
        assert out == {"po_number": "PO-X"}
        entries = get_diff_log()
        assert entries[0].real_error is not None


class TestCanaryTrafficSplit:
    def test_zero_percent_routes_all_to_stub(self):
        from gateways.canary import is_canary_eligible

        for case_id in ("A", "B", "C", "D"):
            assert is_canary_eligible(case_id=case_id, pct=0.0) is False

    def test_hundred_percent_routes_all_to_real(self):
        from gateways.canary import is_canary_eligible

        for case_id in ("A", "B", "C", "D"):
            assert is_canary_eligible(case_id=case_id, pct=1.0) is True

    def test_deterministic_per_case(self):
        from gateways.canary import is_canary_eligible

        # Same case_id, same pct → same routing decision every call.
        for _ in range(100):
            assert is_canary_eligible(case_id="PO-9001", pct=0.5) == \
                is_canary_eligible(case_id="PO-9001", pct=0.5)

    def test_approximate_distribution_at_25_pct(self):
        """Over a large enough sample, ~25% of cases route to real."""
        from gateways.canary import is_canary_eligible

        sample = [f"case-{i}" for i in range(1000)]
        eligible = sum(1 for c in sample if is_canary_eligible(case_id=c, pct=0.25))
        # ±5pp band — hash distributions are not perfectly uniform
        # over small samples.
        assert 200 <= eligible <= 300, f"expected ~250, got {eligible}"


class TestExtractionDriftAlert:
    def test_no_drift_when_containment_stable(self):
        from api.observability.drift_alert import detect_extraction_drift

        recent = [0.92, 0.93, 0.91, 0.92]
        baseline = [0.92, 0.93, 0.91, 0.92, 0.92, 0.91, 0.93]
        result = detect_extraction_drift(
            recent_containments=recent, baseline_containments=baseline,
        )
        assert result.drift_detected is False

    def test_drift_fires_at_5pp_drop(self):
        from api.observability.drift_alert import detect_extraction_drift

        recent = [0.83, 0.84, 0.82]
        baseline = [0.92, 0.93, 0.91, 0.92, 0.92, 0.91, 0.93]
        result = detect_extraction_drift(
            recent_containments=recent, baseline_containments=baseline,
        )
        assert result.drift_detected is True
        assert result.median_drop_pp >= 5.0
        assert result.alert_name == "extraction-drift"

    def test_no_drift_when_recent_is_better(self):
        """An improvement (containment goes UP) is not a drift event."""
        from api.observability.drift_alert import detect_extraction_drift

        recent = [0.96, 0.97, 0.95]
        baseline = [0.90, 0.91, 0.92]
        assert detect_extraction_drift(
            recent_containments=recent, baseline_containments=baseline,
        ).drift_detected is False

    def test_nan_inputs_treated_as_missing(self):
        """Regression for code-review MODERATE finding: a NaN sample
        silently propagates through statistics.median and poisons the
        comparison (NaN >= 5.0 is False), so NaN inputs would
        SUPPRESS the alert. We filter NaNs and only alert on real
        samples; the structured logger flags the missing data
        separately."""
        from api.observability.drift_alert import detect_extraction_drift

        nan = float("nan")
        result = detect_extraction_drift(
            recent_containments=[nan, nan],
            baseline_containments=[0.92, 0.93],
        )
        # No valid recent samples → treated as missing, no alert.
        assert result.drift_detected is False
        # The drop_pp is 0.0 (zeroed when data missing), not NaN.
        import math
        assert not math.isnan(result.median_drop_pp)


class TestBrierCalibration:
    def test_perfect_calibration_zero_brier(self):
        from tests.eval.calibration import brier_score

        # Predictions all 1.0 confidence, all correct → Brier = 0.
        out = brier_score([1.0, 1.0, 1.0], [True, True, True])
        assert out == pytest.approx(0.0)

    def test_random_calibration_high_brier(self):
        from tests.eval.calibration import brier_score

        # 1.0 confidence, all wrong → Brier = 1.
        out = brier_score([1.0, 1.0, 1.0], [False, False, False])
        assert out == pytest.approx(1.0)

    def test_phase7_gate_passes_at_calibrated(self):
        """Phase 7 acceptance: Brier ≤ 0.1 → calibrated."""
        from tests.eval.calibration import brier_score, is_well_calibrated

        # Mix of confident-correct + low-confidence-wrong → Brier well
        # under 0.1.
        confidences = [0.95, 0.93, 0.96, 0.94, 0.05, 0.10]
        correct = [True, True, True, True, False, False]
        b = brier_score(confidences, correct)
        assert b <= 0.1
        assert is_well_calibrated(b) is True

    def test_phase7_gate_fails_at_uncalibrated(self):
        from tests.eval.calibration import brier_score, is_well_calibrated

        # High confidence + frequent miss → Brier high.
        confidences = [0.95, 0.95, 0.95, 0.95]
        correct = [True, False, False, False]
        b = brier_score(confidences, correct)
        assert b > 0.1
        assert is_well_calibrated(b) is False
