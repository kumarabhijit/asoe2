"""PARITY-7 — extraction-drift alert evaluator.

Per ML review: a 7-day rolling median containment drop > 5pp on the
same doc set fires the ``extraction-drift`` named alert (declared in
``api/observability/alerts.py``).

The detector is pure — it takes two sample arrays (recent + baseline
containments) and returns a drift verdict. The caller wires it to
the data source (today: ``api.metrics._extraction_contain``; in GA:
an App Insights query against the rolling window).
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from typing import Sequence

logger = logging.getLogger("asoe.api.drift_alert")


DROP_THRESHOLD_PP = 5.0


@dataclass(frozen=True)
class DriftVerdict:
    alert_name: str
    drift_detected: bool
    median_drop_pp: float
    recent_median: float
    baseline_median: float


def detect_extraction_drift(
    *,
    recent_containments: Sequence[float],
    baseline_containments: Sequence[float],
) -> DriftVerdict:
    """Compare median containment over a recent window vs a baseline.

    Drift is signalled when the recent median is BELOW the baseline
    by at least ``DROP_THRESHOLD_PP`` percentage points. A recent
    median above the baseline (containment improving) is never a
    drift event.

    Empty input arrays return ``drift_detected=False`` — we don't
    alert on missing data; that's the structured-logger's job.
    """
    if not recent_containments or not baseline_containments:
        return DriftVerdict(
            alert_name="extraction-drift",
            drift_detected=False,
            median_drop_pp=0.0,
            recent_median=0.0,
            baseline_median=0.0,
        )

    recent_median = float(statistics.median(recent_containments))
    baseline_median = float(statistics.median(baseline_containments))
    drop_pp = (baseline_median - recent_median) * 100.0

    drift = drop_pp >= DROP_THRESHOLD_PP
    if drift:
        logger.warning(
            "extraction-drift alert recent_median=%.3f baseline_median=%.3f drop_pp=%.2f",
            recent_median, baseline_median, drop_pp,
        )

    return DriftVerdict(
        alert_name="extraction-drift",
        drift_detected=drift,
        median_drop_pp=drop_pp,
        recent_median=recent_median,
        baseline_median=baseline_median,
    )
