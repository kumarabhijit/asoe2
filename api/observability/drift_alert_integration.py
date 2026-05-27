"""PARITY-7 — bridge from metric collector → drift evaluator.

``api.metrics`` records per-extraction containment samples keyed by
(model_id, prompt_hash). This module pulls those samples, splits
them into recent vs baseline windows, and runs the pure
``api.observability.drift_alert.detect_extraction_drift`` evaluator.

The N-most-recent / N-prior split is an approximation of the 7-day
rolling-window contract from the parity plan. In production this is
better served by an App Insights KQL query over a real time window;
this in-process bridge exists so the alert can fire from the same
metric source without an additional ingestion hop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from api.observability.drift_alert import DriftVerdict, detect_extraction_drift


@dataclass(frozen=True)
class KeyedDriftVerdict:
    """Wraps a DriftVerdict with the (model_id, prompt_hash) tuple
    so callers can attribute an alert to the responsible model.
    """

    model_id: str
    prompt_hash: str
    drift_detected: bool
    median_drop_pp: float
    recent_median: float
    baseline_median: float
    alert_name: str = "extraction-drift"


def _verdict_with_key(
    model_id: str, prompt_hash: str, v: DriftVerdict,
) -> KeyedDriftVerdict:
    return KeyedDriftVerdict(
        model_id=model_id,
        prompt_hash=prompt_hash,
        drift_detected=v.drift_detected,
        median_drop_pp=v.median_drop_pp,
        recent_median=v.recent_median,
        baseline_median=v.baseline_median,
        alert_name=v.alert_name,
    )


def evaluate_extraction_drift_for_model(
    *,
    model_id: str,
    prompt_hash: str,
    recent_n: int = 50,
    baseline_n: int = 200,
) -> KeyedDriftVerdict:
    """Run the drift detector against the samples for one model/prompt.

    Splits the recorded samples into recent (last ``recent_n``) and
    baseline (the ``baseline_n`` before that). Insufficient samples on
    either side → no drift signal (drift_detected=False).
    """
    from api import metrics

    samples = list(metrics.snapshot_extraction_metrics().get(
        "contain", {}
    ).get((model_id, prompt_hash), []))
    return _split_and_detect(samples, model_id, prompt_hash, recent_n, baseline_n)


def _split_and_detect(
    samples: List[float],
    model_id: str,
    prompt_hash: str,
    recent_n: int,
    baseline_n: int,
) -> KeyedDriftVerdict:
    if len(samples) < (recent_n + baseline_n) // 2:
        # Not enough samples to draw a baseline AND a recent window.
        v = detect_extraction_drift(
            recent_containments=[], baseline_containments=[],
        )
        return _verdict_with_key(model_id, prompt_hash, v)
    recent = samples[-recent_n:]
    # The "baseline" window precedes the recent window — older samples.
    baseline_start = max(0, len(samples) - recent_n - baseline_n)
    baseline = samples[baseline_start: len(samples) - recent_n]
    if not baseline or not recent:
        v = detect_extraction_drift(
            recent_containments=[], baseline_containments=[],
        )
        return _verdict_with_key(model_id, prompt_hash, v)
    v = detect_extraction_drift(
        recent_containments=recent, baseline_containments=baseline,
    )
    return _verdict_with_key(model_id, prompt_hash, v)


def evaluate_all_extraction_drift(
    *, recent_n: int = 50, baseline_n: int = 200,
) -> List[KeyedDriftVerdict]:
    """Iterate every (model_id, prompt_hash) the collector knows about.

    Callers filter the result list by ``drift_detected=True`` to
    forward to App Insights or the alert manager.
    """
    from api import metrics

    contain = metrics.snapshot_extraction_metrics().get("contain", {})
    out: List[KeyedDriftVerdict] = []
    for (model_id, prompt_hash), samples in sorted(contain.items()):
        out.append(_split_and_detect(
            list(samples), model_id, prompt_hash, recent_n, baseline_n,
        ))
    return out
