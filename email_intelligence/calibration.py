from __future__ import annotations

# ADR-036 D4 — confidence-calibration instrumentation.
#
# A model's confidence may only GATE behaviour once it is calibrated
# (Expected Calibration Error within tolerance). This module records the
# (predicted supergroup, confidence) of every live decision so that, once
# human-confirmed outcomes are joined back in, ECE can be computed and the
# shadow→gate promotion decision is data-driven rather than a guess.
#
# This is instrumentation, not a gate. It is in-memory + callback-based by
# design: durable persistence (a calibration table / metrics sink) is an
# ops wiring concern that varies by deployment. Tests and a future metrics
# exporter consume the same recorder.

import threading
from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass(frozen=True)
class CalibrationRecord:
    """One live prediction awaiting an outcome label. ``correct`` is
    filled later (when a human confirms / reclassifies the case); until
    then it is None and the record is "pending"."""

    predicted_supergroup: str
    confidence: float
    backend_kind: str
    case_ref: Optional[str] = None
    correct: Optional[bool] = None


# Optional sink — an ops integration can register a callback to forward
# records to a durable store / metrics pipeline. Defaults to in-memory only.
_sink: Optional[Callable[[CalibrationRecord], None]] = None
_buffer: List[CalibrationRecord] = []
_lock = threading.Lock()


def set_sink(sink: Optional[Callable[[CalibrationRecord], None]]) -> None:
    """Register (or clear, with None) a durable sink for calibration
    records. Called once at startup by the ops layer if configured."""
    global _sink
    _sink = sink


def record_prediction(
    predicted_supergroup: str,
    confidence: float,
    *,
    backend_kind: str,
    case_ref: Optional[str] = None,
    correct: Optional[bool] = None,
) -> CalibrationRecord:
    """Record one live prediction for later ECE computation."""
    rec = CalibrationRecord(
        predicted_supergroup=predicted_supergroup,
        confidence=confidence,
        backend_kind=backend_kind,
        case_ref=case_ref,
        correct=correct,
    )
    with _lock:
        _buffer.append(rec)
    if _sink is not None:
        try:
            _sink(rec)
        except Exception:  # pragma: no cover - a sink error must not break intake
            pass
    return rec


def buffered_records() -> List[CalibrationRecord]:
    """Snapshot of in-memory records (mainly for tests / a local exporter)."""
    with _lock:
        return list(_buffer)


def reset() -> None:
    """Clear the in-memory buffer (test hygiene)."""
    with _lock:
        _buffer.clear()


def expected_calibration_error(
    records: List[CalibrationRecord], *, n_bins: int = 10
) -> Optional[float]:
    """Standard ECE over records that have a ``correct`` label.

    Bins predictions by confidence into ``n_bins`` equal-width bins and
    returns the weighted average gap between mean confidence and accuracy
    per bin. Returns None when there are no labelled records (you cannot
    calibrate without outcomes) — the honest "not yet measurable" answer,
    not a fabricated 0.0.
    """
    labelled = [r for r in records if r.correct is not None]
    if not labelled:
        return None
    n = len(labelled)
    total = 0.0
    for b in range(n_bins):
        lo = b / n_bins
        hi = (b + 1) / n_bins
        # Last bin is closed on the right so confidence==1.0 lands somewhere.
        in_bin = [
            r for r in labelled
            if (lo < r.confidence <= hi) or (b == 0 and r.confidence <= hi)
        ]
        if not in_bin:
            continue
        avg_conf = sum(r.confidence for r in in_bin) / len(in_bin)
        accuracy = sum(1 for r in in_bin if r.correct) / len(in_bin)
        total += (len(in_bin) / n) * abs(avg_conf - accuracy)
    return total
