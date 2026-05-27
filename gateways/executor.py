from __future__ import annotations

import concurrent.futures
import logging
import time

from contracts.models import GatewayRequest, GatewayResponse
from gateways.circuit_breaker import CircuitOpen, get_breaker, record_call
from gateways.registry import get_gateway

logger = logging.getLogger("asoe.gateways")

# Response statuses that count as an infra failure for the gateway-tier
# circuit breaker (DoR #8). A completed call with any other status is a
# breaker success — the dependency answered.
_BREAKER_FAILURE_STATUSES = frozenset({"FAILED", "UNAVAILABLE", "TIMEOUT"})


class GatewayExecutor:
    """Execute gateway operations with tracing and structured error handling.

    Every call — success or failure — is logged to ``asoe.gateways`` so
    the observability layer can forward it to LangFuse or any aggregator.

    A shared thread pool is reused across calls to avoid OS-level
    thread create/destroy overhead per gateway invocation.

    Pooling note: ``run()`` submits ``gateway.execute`` to this ``_pool`` to
    enforce the per-call timeout. ``orchestration.nodes.resolve_dependencies``
    fans out GatewayDependency calls on its OWN dedicated pool
    (``_DEPENDENCY_FANOUT_POOL``), NOT this one — so the outer fan-out and the
    inner execute submits never compete for the same workers. That decoupling
    removes the former recursive-submit self-deadlock (where N outer + N inner
    tasks contended for one shared pool), so a recipe's dependency count is no
    longer bounded by this pool's size. Do not route the fan-out back through
    ``_pool``.
    """

    _pool = concurrent.futures.ThreadPoolExecutor(max_workers=16)

    def _record(self, name: str, breaker, start: float, *, failed: bool) -> None:
        """Feed one completed-call outcome to the breaker + metering (DoR #8)."""
        elapsed_s = time.monotonic() - start
        if failed:
            breaker.record_failure(elapsed_s)
        else:
            breaker.record_success(elapsed_s)
        record_call(name, failed=failed, short_circuited=False,
                    latency_ms=elapsed_s * 1000.0)

    def run(self, request: GatewayRequest) -> GatewayResponse:
        start = time.monotonic()

        # --- resolve gateway from registry ---
        # A registration miss is a config error, not a transient infra failure,
        # so it does NOT feed the circuit breaker.
        try:
            gateway = get_gateway(request.gateway_name)
        except KeyError:
            return GatewayResponse(
                gateway_name=request.gateway_name,
                operation=request.operation,
                status="UNAVAILABLE",
                error=f"Gateway not registered: {request.gateway_name}",
            )

        # --- circuit breaker gate (DoR #8 — gateway-tier parity) ---
        breaker = get_breaker(request.gateway_name)
        try:
            breaker.acquire()
        except CircuitOpen as exc:
            record_call(request.gateway_name, failed=True,
                        short_circuited=True, latency_ms=0.0)
            logger.warning(
                "gateway_circuit_open",
                extra={
                    "gateway": request.gateway_name,
                    "operation": request.operation,
                    "trace_id": request.trace_id,
                },
            )
            return GatewayResponse(
                gateway_name=request.gateway_name,
                operation=request.operation,
                status="UNAVAILABLE",
                error=f"Gateway circuit breaker OPEN: {exc}",
                duration_ms=0,
            )

        # --- execute with timeout enforcement (SEC-4) ---
        #
        # PARITY-6 — apply the per-gateway timeout budget from
        # contracts/policy.py::GATEWAY_TIMEOUT_S if the caller didn't
        # set an explicit (non-default) timeout. The map enforces
        # per-connector budgets (Graph ~3s, SAP ~8s, OMS ~5s) so a
        # slow upstream can't burn the whole request budget.
        from contracts.policy import GATEWAY_TIMEOUT_S
        per_gateway_budget = GATEWAY_TIMEOUT_S.get(request.gateway_name)
        # The GatewayRequest default is 5000ms; if a caller explicitly
        # set a tighter timeout (e.g. recipe-local override), respect
        # it. Only when the caller is at the default do we tighten to
        # the per-gateway budget.
        if per_gateway_budget is not None and request.timeout_ms == 5000:
            effective_timeout_ms = int(per_gateway_budget * 1000)
        else:
            effective_timeout_ms = request.timeout_ms
        timeout_sec = effective_timeout_ms / 1000.0
        try:
            future = self._pool.submit(gateway.execute, request)
            try:
                response = future.result(timeout=timeout_sec)
            except concurrent.futures.TimeoutError:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                self._record(request.gateway_name, breaker, start, failed=True)
                logger.error(
                    "gateway_timeout",
                    extra={
                        "gateway": request.gateway_name,
                        "operation": request.operation,
                        "timeout_ms": effective_timeout_ms,
                        "trace_id": request.trace_id,
                    },
                )
                return GatewayResponse(
                    gateway_name=request.gateway_name,
                    operation=request.operation,
                    status="TIMEOUT",
                    error=f"Gateway call exceeded {effective_timeout_ms}ms timeout",
                    duration_ms=elapsed_ms,
                )
            elapsed_ms = int((time.monotonic() - start) * 1000)
            response = response.model_copy(update={"duration_ms": elapsed_ms})
            self._record(
                request.gateway_name, breaker, start,
                failed=response.status in _BREAKER_FAILURE_STATUSES,
            )
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
            self._record(request.gateway_name, breaker, start, failed=True)
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
            self._record(request.gateway_name, breaker, start, failed=True)
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
