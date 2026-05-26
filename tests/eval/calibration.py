"""PARITY-7 — confidence calibration check.

The Phase 7 gate: AzureDI's per-field confidence is NOT calibrated to
our containment definition out of the box. Run a calibration sweep on
the expanded golden set; if the Brier score > 0.1, recalibrate or
replace AzureDI confidence with our own ECE-derived score in the live
gate.

Brier = mean((confidence_i - 1{correct_i})^2). 0 = perfect
calibration, 1 = inverted. Threshold 0.1 mirrors the ECE bound used
elsewhere in the eval harness.
"""

from __future__ import annotations

from typing import Sequence


BRIER_GATE = 0.1


def brier_score(confidences: Sequence[float], correct: Sequence[bool]) -> float:
    """Mean squared error of confidence vs binary outcome.

    Both inputs must be the same length; an empty input returns 0.0
    (no data → no calibration claim).
    """
    if len(confidences) != len(correct):
        raise ValueError(
            f"brier_score: length mismatch confidences={len(confidences)} "
            f"correct={len(correct)}"
        )
    if not confidences:
        return 0.0
    n = len(confidences)
    s = 0.0
    for c, ok in zip(confidences, correct):
        target = 1.0 if ok else 0.0
        s += (float(c) - target) ** 2
    return s / n


def is_well_calibrated(brier: float) -> bool:
    """``True`` iff the Brier score is at or below the Phase 7 gate.

    Callers should fall back to ECE-derived confidence (or block the
    live flip) when this returns False.
    """
    return brier <= BRIER_GATE
