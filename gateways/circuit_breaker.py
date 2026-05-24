from __future__ import annotations

# Gateway-tier circuit breaker + metering (DoR #8 — parity with the LLM tier).
#
# Brings the same textbook 3-state breaker the LLM router uses
# (`llm/circuit_breaker.LLMCircuitBreaker`) to the infrastructure gateways
# (email-intake, SAP order/doc/contract/master-data, OMS, ERP, …). A breaker is
# kept per `gateway_name`, so one flaky dependency trips in isolation without
# starving the others. The breaker is consulted at the single
# `gateways.executor.GatewayExecutor.run` chokepoint:
#
#   * CLOSED   — call the gateway; record the outcome.
#   * OPEN     — short-circuit to an UNAVAILABLE GatewayResponse WITHOUT calling
#                the gateway (fail fast; the dependency is unhealthy).
#   * HALF_OPEN — let a single probe through; success closes, failure re-opens.
#
# Metering mirrors the LLM tier: per-gateway call / failure / short-circuit
# counters + cumulative latency, rendered onto the Prometheus surface.
#
# All state is process-local and resettable (`reset_all`) so tests don't leak
# breaker trips across cases — the conftest autouse fixture calls it.

import threading
from dataclasses import dataclass, field
from typing import Callable, Dict

from contracts.policy import (
    GATEWAY_CIRCUIT_BREAKER_COOLDOWN_S,
    GATEWAY_CIRCUIT_BREAKER_ERROR_RATE_PCT,
    GATEWAY_CIRCUIT_BREAKER_P95_LATENCY_S,
)
from llm.circuit_breaker import BreakerSnapshot, CircuitOpen, LLMCircuitBreaker

__all__ = [
    "CircuitOpen",
    "get_breaker",
    "record_call",
    "reset_all",
    "breaker_snapshots",
    "metering_snapshot",
    "GatewayMeter",
]

_lock = threading.Lock()
_breakers: Dict[str, LLMCircuitBreaker] = {}


def _new_breaker() -> LLMCircuitBreaker:
    return LLMCircuitBreaker(
        error_rate_threshold=GATEWAY_CIRCUIT_BREAKER_ERROR_RATE_PCT,
        p95_latency_threshold_s=GATEWAY_CIRCUIT_BREAKER_P95_LATENCY_S,
        cooldown_s=GATEWAY_CIRCUIT_BREAKER_COOLDOWN_S,
    )


def get_breaker(gateway_name: str) -> LLMCircuitBreaker:
    """Return the (lazily created) breaker for a gateway. One per name."""
    with _lock:
        breaker = _breakers.get(gateway_name)
        if breaker is None:
            breaker = _new_breaker()
            _breakers[gateway_name] = breaker
        return breaker


@dataclass
class GatewayMeter:
    """Per-gateway call metering (parity with the LLM-tier metering)."""

    calls: int = 0
    failures: int = 0
    short_circuits: int = 0  # calls rejected because the breaker was OPEN
    latency_ms_sum: float = 0.0


_meters: Dict[str, GatewayMeter] = {}


def _meter(gateway_name: str) -> GatewayMeter:
    meter = _meters.get(gateway_name)
    if meter is None:
        meter = GatewayMeter()
        _meters[gateway_name] = meter
    return meter


def record_call(
    gateway_name: str, *, failed: bool, short_circuited: bool, latency_ms: float
) -> None:
    """Record one gateway invocation outcome for the Prometheus surface."""
    with _lock:
        m = _meter(gateway_name)
        if short_circuited:
            m.short_circuits += 1
            return
        m.calls += 1
        m.latency_ms_sum += max(0.0, latency_ms)
        if failed:
            m.failures += 1


def reset_all() -> None:
    """Test helper — drop every breaker + meter (process-local)."""
    with _lock:
        _breakers.clear()
        _meters.clear()


def breaker_snapshots() -> Dict[str, BreakerSnapshot]:
    with _lock:
        return {name: b.snapshot() for name, b in _breakers.items()}


def metering_snapshot() -> Dict[str, GatewayMeter]:
    with _lock:
        return {name: GatewayMeter(**vars(m)) for name, m in _meters.items()}
