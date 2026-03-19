from __future__ import annotations

import logging
import time

from contracts.models import GatewayRequest, GatewayResponse
from gateways.registry import get_gateway

logger = logging.getLogger("asoe.gateways")


class GatewayExecutor:
    """Execute gateway operations with tracing and structured error handling.

    Every call — success or failure — is logged to ``asoe.gateways`` so
    the observability layer can forward it to LangFuse or any aggregator.
    """

    def run(self, request: GatewayRequest) -> GatewayResponse:
        start = time.monotonic()

        # --- resolve gateway from registry ---
        try:
            gateway = get_gateway(request.gateway_name)
        except KeyError:
            return GatewayResponse(
                gateway_name=request.gateway_name,
                operation=request.operation,
                status="UNAVAILABLE",
                error=f"Gateway not registered: {request.gateway_name}",
            )

        # --- execute and trace ---
        try:
            response = gateway.execute(request)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            response = response.model_copy(update={"duration_ms": elapsed_ms})
            logger.info(
                "gateway_call",
                extra={
                    "gateway": request.gateway_name,
                    "operation": request.operation,
                    "status": response.status,
                    "duration_ms": elapsed_ms,
                    "trace_id": request.trace_id,
                },
            )
            return response
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.error(
                "gateway_error",
                extra={
                    "gateway": request.gateway_name,
                    "operation": request.operation,
                    "error": str(exc),
                    "trace_id": request.trace_id,
                },
            )
            return GatewayResponse(
                gateway_name=request.gateway_name,
                operation=request.operation,
                status="FAILED",
                error=str(exc),
                duration_ms=elapsed_ms,
            )
