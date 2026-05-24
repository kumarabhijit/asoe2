"""DoR gate #8 — gateway-tier circuit breaker + metering (LLM-tier parity).

The breaker lives at the single `GatewayExecutor.run` chokepoint, one instance
per gateway_name. These tests drive a deliberately-failing gateway to trip the
breaker, prove tripped calls short-circuit to UNAVAILABLE WITHOUT invoking the
gateway, and that the cooldown → HALF_OPEN → CLOSED recovery works. Metering
parity (calls / failures / short-circuits) is locked too. Deterministic — a
fake clock drives the cooldown; no real sleeps, no live infra.
"""

from __future__ import annotations

import pytest

from contracts.models import GatewayRequest, GatewayResponse
from contracts.policy import GATEWAY_CIRCUIT_BREAKER_COOLDOWN_S
from gateways import circuit_breaker as cb
from gateways.executor import GatewayExecutor
from gateways.registry import clear_registry, register_gateway


class _FlakyGateway:
    """Stub whose execute() outcome is scripted per call; counts invocations."""

    def __init__(self, name: str, statuses: list[str]) -> None:
        self._name = name
        self._statuses = statuses
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    def execute(self, request: GatewayRequest) -> GatewayResponse:
        idx = min(self.calls, len(self._statuses) - 1)
        status = self._statuses[idx]
        self.calls += 1
        return GatewayResponse(
            gateway_name=self._name, operation=request.operation, status=status,
        )

    def health_check(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _clean():
    clear_registry()
    cb.reset_all()
    yield
    clear_registry()
    cb.reset_all()


def _req(name: str) -> GatewayRequest:
    return GatewayRequest(
        gateway_name=name, operation="op", params={}, trace_id="t-1",
        timeout_ms=1000,
    )


def test_breaker_trips_after_repeated_failures_and_short_circuits():
    gw = _FlakyGateway("flaky", ["FAILED"] * 50)
    register_gateway(gw)
    ex = GatewayExecutor()

    # 5 failures (MIN_SAMPLES) at 100% error rate trips the breaker.
    for _ in range(5):
        resp = ex.run(_req("flaky"))
        assert resp.status == "FAILED"
    calls_before_open = gw.calls

    # Next call is short-circuited: UNAVAILABLE, breaker OPEN, gateway NOT hit.
    resp = ex.run(_req("flaky"))
    assert resp.status == "UNAVAILABLE"
    assert "circuit breaker OPEN" in (resp.error or "")
    assert gw.calls == calls_before_open  # the gateway was not invoked

    meters = cb.metering_snapshot()["flaky"]
    assert meters.failures == 5
    assert meters.short_circuits >= 1


def test_healthy_gateway_keeps_breaker_closed():
    gw = _FlakyGateway("steady", ["SUCCESS"] * 50)
    register_gateway(gw)
    ex = GatewayExecutor()
    for _ in range(20):
        assert ex.run(_req("steady")).status == "SUCCESS"
    assert cb.breaker_snapshots()["steady"].state.value == "closed"
    assert gw.calls == 20


def test_cooldown_then_probe_recovers_to_closed():
    # Drive the breaker with a fake clock so the cooldown elapses deterministically.
    clock = {"t": 1000.0}
    breaker = cb.get_breaker("recovers")
    breaker._clock = lambda: clock["t"]  # noqa: SLF001 - test-controlled clock

    gw = _FlakyGateway("recovers", ["FAILED"] * 5 + ["SUCCESS"] * 5)
    register_gateway(gw)
    ex = GatewayExecutor()

    for _ in range(5):
        ex.run(_req("recovers"))
    assert breaker.snapshot().state.value == "open"

    # Advance past the cooldown → next acquire moves to HALF_OPEN (probe).
    clock["t"] += GATEWAY_CIRCUIT_BREAKER_COOLDOWN_S + 1
    resp = ex.run(_req("recovers"))  # probe succeeds → CLOSED
    assert resp.status == "SUCCESS"
    assert breaker.snapshot().state.value == "closed"


def test_unregistered_gateway_does_not_engage_breaker():
    ex = GatewayExecutor()
    resp = ex.run(_req("nonexistent"))
    assert resp.status == "UNAVAILABLE"
    assert "not registered" in (resp.error or "")
    # No breaker/meter created for a config miss.
    assert "nonexistent" not in cb.metering_snapshot()
