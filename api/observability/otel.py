"""PARITY-5 — OpenTelemetry FastAPI instrumentation + Azure Monitor export.

Initialisation is gated on ``APPLICATIONINSIGHTS_CONNECTION_STRING``.
When the env var is unset (local dev, CI, vitest browser flows), the
module is a deliberate no-op — no heavy imports, no exporter, no
startup latency. When the var IS set AND the otel + azure-monitor
packages are installed, ``init_telemetry`` wires:

  * FastAPI instrumentation (request span per route)
  * OTLP HTTP exporter to App Insights
  * Resource attributes derived from ASOE_ENV + container revision

Library imports are lazy so a deploy without the optional deps still
boots — ``init_telemetry`` returns ``False`` and logs a one-line
warning rather than crashing the worker. The Phase 5 deploy script
ensures the libs are present; this seam keeps the test matrix sane.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("asoe.api.otel")


def _connection_string() -> str:
    return (os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING") or "").strip()


def init_telemetry(app: Optional[Any] = None) -> bool:
    """Initialise OTel + Azure Monitor exporters.

    Returns ``True`` when initialisation actually wired exporters,
    ``False`` when it was a deliberate no-op (env unset OR libs not
    installed). Never raises — observability failures must not
    crash the worker on startup.

    ``app`` is the FastAPI application instance; when provided we
    attach the FastAPI instrumentation. When ``None`` we still
    initialise the SDK so non-HTTP code paths (graph nodes, background
    tasks) emit spans.
    """
    conn = _connection_string()
    if not conn:
        return False

    try:
        # Lazy import — otel libs are an optional dep so a Vercel /
        # CI worker without them still boots.
        from azure.monitor.opentelemetry import configure_azure_monitor  # type: ignore
    except ImportError:
        logger.warning(
            "APPLICATIONINSIGHTS_CONNECTION_STRING is set but the "
            "azure-monitor-opentelemetry package is not installed; "
            "telemetry will not be exported"
        )
        return False

    try:
        configure_azure_monitor(
            connection_string=conn,
            resource_attributes={
                "service.name": "asoe-api",
                "service.namespace": "asoe",
                "deployment.environment": os.getenv("ASOE_ENV", "preprod"),
            },
        )
    except Exception as exc:  # noqa: BLE001 — observability must not crash worker
        logger.warning("azure-monitor configure failed: %s", exc)
        return False

    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # type: ignore
            FastAPIInstrumentor.instrument_app(app)
        except ImportError:
            logger.info(
                "FastAPI OTel instrumentation skipped — install "
                "opentelemetry-instrumentation-fastapi to enable"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("FastAPI OTel instrumentation failed: %s", exc)

    logger.info("OTel + Azure Monitor exporters initialised")
    return True
