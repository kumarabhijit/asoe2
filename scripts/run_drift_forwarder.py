"""PARITY-7 followup — drift-alert App Insights forwarder.

The drift detector at
``api.observability.drift_alert_integration.evaluate_all_extraction_drift``
runs purely in-process. This CLI is the Container Apps Job glue that
fires on a cron schedule, runs the evaluator, and emits a structured
``extraction-drift`` log event per (model_id, prompt_hash) with
``drift_detected=True``. The events flow through stdlib logging; in
preprod the OTel handler installed by
``api.observability.otel.init_telemetry`` exports them to App
Insights when ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is set.

Bicep: ``infra/main.bicep::driftAlertJob`` provisions the Job;
default OFF (``deployDriftAlertJob=false``). The Job reuses the API
container image so this script ships in the same artifact the API
worker runs.

Exit codes:
  * 0 — evaluator ran, results logged (zero drifts is success).
  * 1 — evaluator raised. Container Apps surfaces this as a failed
    execution so the operator gets paged.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import List

logger = logging.getLogger("asoe.scripts.run_drift_forwarder")


def _recent_n() -> int:
    raw = (os.getenv("ASOE_DRIFT_RECENT_N") or "50").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 50


def _baseline_n() -> int:
    raw = (os.getenv("ASOE_DRIFT_BASELINE_N") or "200").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 200


def _evaluate_all() -> List:
    """Indirection so tests can monkeypatch evaluator failures.

    Returns the list of KeyedDriftVerdict the integration layer
    produces — the forwarder filters for ``drift_detected=True``.
    """
    from api.observability.drift_alert_integration import (
        evaluate_all_extraction_drift,
    )
    return evaluate_all_extraction_drift(
        recent_n=_recent_n(), baseline_n=_baseline_n(),
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Initialise OTel exporters when App Insights is configured so
    # the per-drift log events flow to the Azure-side Log Analytics
    # workspace. Safe no-op when the connection string is unset.
    try:
        from api.observability.otel import init_telemetry
        init_telemetry()
    except Exception:  # noqa: BLE001
        # OTel init is best-effort; failures don't block the
        # evaluator. The structured log on stdout still gets captured
        # by `az containerapp job logs show`.
        logger.warning("init_telemetry failed; falling back to stdout only")

    try:
        verdicts = _evaluate_all()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "drift_forwarder_error reason=%s err=%s",
            type(exc).__name__, exc,
        )
        return 1

    drift_count = 0
    for v in verdicts:
        if getattr(v, "drift_detected", False):
            drift_count += 1
            # Emit the canonical alert name in the log message so
            # App Insights / Log Analytics queries
            # `WHERE message contains "extraction-drift"` work.
            logger.warning(
                "extraction-drift model_id=%s prompt_hash=%s "
                "median_drop_pp=%.4f recent_median=%.4f baseline_median=%.4f",
                getattr(v, "model_id", "unknown"),
                getattr(v, "prompt_hash", "unknown"),
                getattr(v, "median_drop_pp", 0.0),
                getattr(v, "recent_median", 0.0),
                getattr(v, "baseline_median", 0.0),
            )

    logger.info(
        "drift_forwarder_done evaluated=%d drift_count=%d",
        len(verdicts), drift_count,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
