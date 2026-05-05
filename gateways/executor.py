from __future__ import annotations

import concurrent.futures
import logging
import time

from contracts.models import GatewayRequest, GatewayResponse
from gateways.registry import get_gateway

logger = logging.getLogger("asoe.gateways")


class GatewayExecutor:
    """Execute gateway operations with tracing and structured error handling.

    Every call — success or failure — is logged to ``asoe.gateways`` so
    the observability layer can forward it to LangFuse or any aggregator.

    A shared thread pool is reused across calls to avoid OS-level
    thread create/destroy overhead per gateway invocation.

    Sizing note: ``orchestration.nodes.resolve_dependencies`` fans out
    GatewayDependency calls by submitting to this same ``_pool`` AND
    each ``run()`` here also submits to ``_pool`` to enforce the
    per-call timeout. With N dependencies the worst case is N outer
    + N inner tasks competing for the pool — classic recursive-submit
    deadlock when ``max_workers <= N``. The pool is sized at 16 so
    recipes with up to ~7 declared dependencies stay safely above the
    deadlock floor (current max is EmailOrderEntryRecipe at 4). If a
    future recipe needs more, the right fix is to give the outer
    fan-out its own pool — not to keep bumping this constant.
    """

    _pool = concurrent.futures.ThreadPoolExecutor(max_workers=16)

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

        # --- execute with timeout enforcement (SEC-4) ---
        timeout_sec = request.timeout_ms / 1000.0
        try:
            future = self._pool.submit(gateway.execute, request)
            try:
                response = future.result(timeout=timeout_sec)
            except concurrent.futures.TimeoutError:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                logger.error(
                    "gateway_timeout",
                    extra={
                        "gateway": request.gateway_name,
                        "operation": request.operation,
                        "timeout_ms": request.timeout_ms,
                        "trace_id": request.trace_id,
                    },
                )
                return GatewayResponse(
                    gateway_name=request.gateway_name,
                    operation=request.operation,
                    status="TIMEOUT",
                    error=f"Gateway call exceeded {request.timeout_ms}ms timeout",
                    duration_ms=elapsed_ms,
                )
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
        except (RuntimeError, ValueError, TypeError, OSError, KeyError) as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.error(
                "gateway_error",
                extra={
                    "gateway": request.gateway_name,
                    "operation": request.operation,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "trace_id": request.trace_id,
                    "duration_ms": elapsed_ms,
                },
            )
            return GatewayResponse(
                gateway_name=request.gateway_name,
                operation=request.operation,
                status="FAILED",
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=elapsed_ms,
            )
        except Exception as exc:
            # Catch-all for truly unexpected errors — logged at critical level
            # so they are visible in monitoring dashboards.
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.critical(
                "gateway_unexpected_error",
                extra={
                    "gateway": request.gateway_name,
                    "operation": request.operation,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "trace_id": request.trace_id,
                    "duration_ms": elapsed_ms,
                },
            )
            return GatewayResponse(
                gateway_name=request.gateway_name,
                operation=request.operation,
                status="FAILED",
                error=f"Unexpected {type(exc).__name__}: {exc}",
                duration_ms=elapsed_ms,
            )
