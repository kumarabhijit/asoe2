from __future__ import annotations

# Coverage for llm/circuit_breaker.py
#
# The breaker is sliding-window over 60s. We control time via an
# injected `clock` so tests are deterministic — no sleeps, no flake.
#
# Verifies:
#   - Starts CLOSED
#   - Records below MIN_SAMPLES do not trip
#   - Error rate ≥ threshold trips → OPEN
#   - p95 latency ≥ threshold trips → OPEN
#   - acquire() raises CircuitOpen while OPEN
#   - Cooldown expiry → HALF_OPEN; one probe permitted
#   - Probe success closes; probe failure re-opens with reset cooldown
#   - reset() returns to CLOSED with no history

import pytest

from llm.circuit_breaker import (
    BreakerState,
    CircuitOpen,
    LLMCircuitBreaker,
    get_circuit_breaker,
    reset_circuit_breaker,
)


class _FakeClock:
    """Monotonic clock under test control. tick(seconds) advances."""

    def __init__(self, t: float = 0.0):
        self._t = t

    def __call__(self) -> float:
        return self._t

    def tick(self, dt: float) -> None:
        self._t += dt


def _breaker(error_rate: float = 0.25, p95_s: float = 15.0, cooldown_s: int = 300) -> tuple[LLMCircuitBreaker, _FakeClock]:
    clock = _FakeClock()
    return (
        LLMCircuitBreaker(
            error_rate_threshold=error_rate,
            p95_latency_threshold_s=p95_s,
            cooldown_s=cooldown_s,
            clock=clock,
        ),
        clock,
    )


# ---------------------------------------------------------------------------
# CLOSED → OPEN transitions
# ---------------------------------------------------------------------------


def test_starts_closed_and_acquires() -> None:
    cb, _ = _breaker()
    assert cb.snapshot().state is BreakerState.CLOSED
    cb.acquire()  # does not raise


def test_below_min_samples_does_not_trip() -> None:
    cb, _ = _breaker()
    # 3 samples, 100% error — but below MIN_SAMPLES=5, no trip.
    for _ in range(3):
        cb.record_failure(0.5)
    assert cb.snapshot().state is BreakerState.CLOSED
    cb.acquire()  # still allowed


def test_error_rate_trips_breaker() -> None:
    cb, _ = _breaker(error_rate=0.25)
    # 6 samples, 50% error rate → trip
    for _ in range(3):
        cb.record_success(0.5)
    for _ in range(3):
        cb.record_failure(0.5)
    snap = cb.snapshot()
    assert snap.state is BreakerState.OPEN
    assert "error_rate" in (snap.last_trip_reason or "")


def test_p95_latency_trips_breaker() -> None:
    cb, _ = _breaker(p95_s=15.0)
    # 10 successes, all >15s → p95 trips even with zero error rate
    for _ in range(10):
        cb.record_success(20.0)
    snap = cb.snapshot()
    assert snap.state is BreakerState.OPEN
    assert "p95_latency" in (snap.last_trip_reason or "")


def test_acquire_raises_when_open() -> None:
    cb, _ = _breaker()
    for _ in range(10):
        cb.record_failure(0.1)
    with pytest.raises(CircuitOpen):
        cb.acquire()


# ---------------------------------------------------------------------------
# Cooldown + HALF_OPEN
# ---------------------------------------------------------------------------


def test_cooldown_transitions_to_half_open() -> None:
    cb, clock = _breaker(cooldown_s=300)
    for _ in range(10):
        cb.record_failure(0.1)
    assert cb.snapshot().state is BreakerState.OPEN

    clock.tick(299)
    with pytest.raises(CircuitOpen):
        cb.acquire()  # still in cooldown

    clock.tick(2)  # 301 total
    # First acquire after cooldown is permitted as a probe (state
    # stays HALF_OPEN with probe_in_flight=True)
    cb.acquire()
    snap = cb.snapshot()
    assert snap.state is BreakerState.HALF_OPEN

    # Second concurrent acquire while probe is still in flight is
    # rejected — only ONE probe at a time.
    with pytest.raises(CircuitOpen):
        cb.acquire()


def test_probe_success_closes_breaker() -> None:
    cb, clock = _breaker(cooldown_s=10)
    for _ in range(10):
        cb.record_failure(0.1)
    clock.tick(11)
    cb.acquire()  # probe granted
    cb.record_success(0.5)  # probe succeeded
    assert cb.snapshot().state is BreakerState.CLOSED


def test_probe_failure_reopens_with_reset_cooldown() -> None:
    cb, clock = _breaker(cooldown_s=10)
    for _ in range(10):
        cb.record_failure(0.1)
    clock.tick(11)
    cb.acquire()  # probe granted
    cb.record_failure(0.5)  # probe failed — back to OPEN
    snap = cb.snapshot()
    assert snap.state is BreakerState.OPEN
    # Cooldown should be active again
    assert snap.cooldown_remaining_s > 0


# ---------------------------------------------------------------------------
# Sliding window + reset
# ---------------------------------------------------------------------------


def test_records_outside_window_evicted() -> None:
    cb, clock = _breaker()
    for _ in range(5):
        cb.record_failure(0.1)
    # Trip
    assert cb.snapshot().state is BreakerState.OPEN

    # Move past the 60s window AND the 300s cooldown so the breaker
    # transitions to HALF_OPEN and old records are evicted on the next
    # snapshot/acquire path.
    clock.tick(310)
    snap = cb.snapshot()
    # Old records evicted
    assert snap.samples == 0


def test_reset_returns_closed() -> None:
    cb, _ = _breaker()
    for _ in range(10):
        cb.record_failure(0.1)
    cb.reset()
    snap = cb.snapshot()
    assert snap.state is BreakerState.CLOSED
    assert snap.samples == 0
    assert snap.last_trip_reason is None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_singleton_is_stable() -> None:
    reset_circuit_breaker()
    a = get_circuit_breaker()
    b = get_circuit_breaker()
    assert a is b
    reset_circuit_breaker()
    c = get_circuit_breaker()
    assert c is not a


# ---------------------------------------------------------------------------
# Snapshot fields
# ---------------------------------------------------------------------------


def test_snapshot_reports_error_rate_and_p95() -> None:
    cb, _ = _breaker()
    cb.record_success(0.5)
    cb.record_success(0.6)
    cb.record_failure(0.7)
    snap = cb.snapshot()
    assert snap.samples == 3
    assert snap.error_rate == pytest.approx(1 / 3)
    # p95 of [0.5, 0.6, 0.7] at idx round(0.95*2)=2 → 0.7
    assert snap.p95_latency_s == pytest.approx(0.7)
