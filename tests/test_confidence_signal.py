"""Unit tests for ConfidenceSignal.from_raw (ADR-032 trust surface).

The projector restates a raw classifier score as a typed signal carrying its
calibration provenance. It must (a) mark the served score uncalibrated until
the ADR-032 graduation loop ships, (b) never fabricate a signal for a missing
or non-positive score, and (c) clamp into [0, 1].
"""

from __future__ import annotations

from api.schemas import AnalysisResponse, ConfidenceSignal


def test_from_raw_projects_uncalibrated_signal() -> None:
    sig = ConfidenceSignal.from_raw(0.87, method="llm_intent_classifier_raw")
    assert sig is not None
    assert sig.value == 0.87
    # Served score is uncalibrated until ADR-032 ships — never asserted True.
    assert sig.calibrated is False
    assert sig.method == "llm_intent_classifier_raw"
    assert sig.sample_n is None


def test_from_raw_returns_none_for_missing_or_non_positive() -> None:
    # Mirrors the AnalysisResponse.confidence "stay at 0, UI hides the bar"
    # rule — no fabricated mid-range default.
    assert ConfidenceSignal.from_raw(None, method="m") is None
    assert ConfidenceSignal.from_raw(0, method="m") is None
    assert ConfidenceSignal.from_raw(-0.3, method="m") is None
    assert ConfidenceSignal.from_raw("0.9", method="m") is None
    # A bool is not a confidence score, even though bool is an int subclass.
    assert ConfidenceSignal.from_raw(True, method="m") is None


def test_from_raw_clamps_into_unit_range() -> None:
    assert ConfidenceSignal.from_raw(1.4, method="m").value == 1.0  # type: ignore[union-attr]


def test_analysis_response_defaults_signal_to_none() -> None:
    # Additive + optional: existing constructors stay valid (Guardrail #7).
    resp = AnalysisResponse(diagnosis="d", confidence=0, risk="low", resolution="x")
    assert resp.confidence_signal is None
