from __future__ import annotations

# LLM-tier circuit breaker.
#
# Distinct from the $10k batch-variance / 50-update breaker in
# orchestration/utils.py — that one bounds *business risk* (runaway
# pricing updates). This one bounds *availability risk* of a remote
# dependency: if Anthropic returns 5xx or rate-limits us at scale, we
# should stop trying for 5 minutes and serve every call from the
# deterministic fallback during the cooldown.
#
# Three states (textbook breaker):
#   CLOSED    — allow calls; observe outcomes.
#   OPEN      — short-circuit to fallback; do not call the LLM.
#   HALF_OPEN — allow a single probe; on success → CLOSED; on failure
#               → OPEN with reset cooldown.
#
# Trip conditions (read from contracts/policy.py at construction
# time):
#   - Rolling 60-second error rate ≥ LLM_CIRCUIT_BREAKER_ERROR_RATE_PCT
#   - Rolling 60-second p95 latency ≥ LLM_CIRCUIT_BREAKER_P95_LATENCY_S
# Either condition trips the breaker; both must be observed across at
# least MIN_SAMPLES calls so the breaker doesn't trip on a single
# slow request after a long quiet period.
#
# Single instance per process; backed by an in-memory sliding window.
# Multi-pod consistency is not required — each pod independently
# observes its own dependency health, which is the right behavior:
# one wedged pod should not pull others down with it.

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Deque

from contracts.policy import (
    LLM_CIRCUIT_BREAKER_COOLDOWN_S,
    LLM_CIRCUIT_BREAKER_ERROR_RATE_PCT,
    LLM_CIRCUIT_BREAKER_P95_LATENCY_S,
)

logger = logging.getLogger("asoe.llm.circuit_breaker")


_WINDOW_SECONDS: float = 60.0
"""Sliding window for error-rate and latency calculations."""

_MIN_SAMPLES: int = 5
"""Minimum number of recent calls required before the breaker can
trip on rate-based thresholds. Below this, latency / error rate are
too noisy to act on."""


class BreakerState(Enum):
    CLOSED = "closed"        # Normal operation; LLM calls allowed.
    OPEN = "open"            # Tripped; serve fallback. Cooldown counting down.
    HALF_OPEN = "half_open"  # Probe in flight; one call permitted.


@dataclass(frozen=True)
class BreakerSnapshot:
    """Read-only view used in telemetry."""

    state: BreakerState
    samples: int
    error_rate: float
    p95_latency_s: float
    cooldown_remaining_s: float
    last_trip_reason: str | None


class CircuitOpen(RuntimeError):
    """Raised when a caller asks the breaker for permission and the
    breaker is OPEN. The router catches this and falls through to
    DeterministicFallbackBackend without calling the remote LLM."""


@dataclass
class _CallRecord:
    timestamp: float
    duration_s: float
    error: bool


class LLMCircuitBreaker:
    """Sliding-window breaker for the LLM tier.

    Thread-safe. Process-local. The router calls `acquire()` before
    each LLM attempt; the backend calls `record_success(latency_s)`
    on a 2xx and `record_failure(latency_s)` on any caught exception.
    """

    def __init__(
        self,
        error_rate_threshold: float = LLM_CIRCUIT_BREAKER_ERROR_RATE_PCT,
        p95_latency_threshold_s: float = LLM_CIRCUIT_BREAKER_P95_LATENCY_S,
        cooldown_s: int = LLM_CIRCUIT_BREAKER_COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._error_rate_threshold = error_rate_threshold
        self._p95_latency_threshold_s = p95_latency_threshold_s
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._lock = threading.Lock()

        self._records: Deque[_CallRecord] = deque()
        self._state = BreakerState.CLOSED
        self._opened_at: float | None = None
        self._last_trip_reason: str | None = None
        self._probe_in_flight = False

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def acquire(self) -> None:
        """Permission check before an LLM call.

        Raises CircuitOpen if the breaker is OPEN (or HALF_OPEN with a
        probe already in flight). Caller must catch and route to the
        deterministic fallback.
        """
        with self._lock:
            self._maybe_transition_locked()
            if self._state is BreakerState.CLOSED:
                return
            if self._state is BreakerState.HALF_OPEN and not self._probe_in_flight:
                # Allow exactly one probe. Subsequent acquires fall
                # through to the OPEN/HALF_OPEN-with-probe-in-flight
                # branch below until the probe resolves.
                self._probe_in_flight = True
                return
            raise CircuitOpen(
                f"LLM circuit breaker is OPEN; reason={self._last_trip_reason!r}"
            )

    def record_success(self, latency_s: float) -> None:
        with self._lock:
            self._records.append(_CallRecord(self._clock(), latency_s, False))
            self._evict_old_locked()
            # Probe semantics: HALF_OPEN with in-flight probe + success
            # → CLOSED. A success while OPEN (no probe in flight) is
            # anomalous (acquire() should have rejected) but harmless;
            # leave state alone so cooldown is respected.
            if self._state is BreakerState.HALF_OPEN and self._probe_in_flight:
                self._state = BreakerState.CLOSED
                self._opened_at = None
                self._probe_in_flight = False
                logger.info(
                    "llm.circuit_breaker.closed",
                    extra={"reason": "probe_succeeded"},
                )
            elif self._state is BreakerState.CLOSED:
                self._maybe_trip_locked()

    def record_failure(self, latency_s: float) -> None:
        with self._lock:
            self._records.append(_CallRecord(self._clock(), latency_s, True))
            self._evict_old_locked()
            if self._state is BreakerState.HALF_OPEN and self._probe_in_flight:
                # Probe failed → re-open with reset cooldown.
                self._state = BreakerState.OPEN
                self._opened_at = self._clock()
                self._probe_in_flight = False
                self._last_trip_reason = "probe_failed"
                logger.warning(
                    "llm.circuit_breaker.opened",
                    extra={"reason": self._last_trip_reason},
                )
                return
            self._maybe_trip_locked()

    def snapshot(self) -> BreakerSnapshot:
        with self._lock:
            self._evict_old_locked()
            err_rate, p95 = self._stats_locked()
            cooldown_remaining = self._cooldown_remaining_locked()
            return BreakerSnapshot(
                state=self._state,
                samples=len(self._records),
                error_rate=err_rate,
                p95_latency_s=p95,
                cooldown_remaining_s=cooldown_remaining,
                last_trip_reason=self._last_trip_reason,
            )

    def reset(self) -> None:
        """Test helper. Returns the breaker to CLOSED with empty history."""
        with self._lock:
            self._records.clear()
            self._state = BreakerState.CLOSED
            self._opened_at = None
            self._last_trip_reason = None

    # ------------------------------------------------------------------
    # Internals (lock-held)
    # ------------------------------------------------------------------

    def _evict_old_locked(self) -> None:
        cutoff = self._clock() - _WINDOW_SECONDS
        while self._records and self._records[0].timestamp < cutoff:
            self._records.popleft()

    def _stats_locked(self) -> tuple[float, float]:
        if not self._records:
            return 0.0, 0.0
        n = len(self._records)
        errors = sum(1 for r in self._records if r.error)
        err_rate = errors / n
        # p95: simple sorted-index. n is bounded by ~rate × window so
        # this is fine.
        latencies = sorted(r.duration_s for r in self._records)
        idx = max(0, int(round(0.95 * (n - 1))))
        return err_rate, latencies[idx]

    def _maybe_trip_locked(self) -> None:
        if self._state is not BreakerState.CLOSED:
            return
        if len(self._records) < _MIN_SAMPLES:
            return
        err_rate, p95 = self._stats_locked()
        if err_rate >= self._error_rate_threshold:
            self._state = BreakerState.OPEN
            self._opened_at = self._clock()
            self._last_trip_reason = (
                f"error_rate {err_rate:.2%} >= {self._error_rate_threshold:.2%}"
            )
            logger.warning(
                "llm.circuit_breaker.opened",
                extra={"reason": self._last_trip_reason, "samples": len(self._records)},
            )
            return
        if p95 >= self._p95_latency_threshold_s:
            self._state = BreakerState.OPEN
            self._opened_at = self._clock()
            self._last_trip_reason = (
                f"p95_latency {p95:.2f}s >= {self._p95_latency_threshold_s:.2f}s"
            )
            logger.warning(
                "llm.circuit_breaker.opened",
                extra={"reason": self._last_trip_reason, "samples": len(self._records)},
            )

    def _maybe_transition_locked(self) -> None:
        if self._state is not BreakerState.OPEN:
            return
        if self._opened_at is None:
            return
        if self._clock() - self._opened_at >= self._cooldown_s:
            self._state = BreakerState.HALF_OPEN
            logger.info(
                "llm.circuit_breaker.half_open",
                extra={"cooldown_s": self._cooldown_s},
            )

    def _cooldown_remaining_locked(self) -> float:
        if self._state is not BreakerState.OPEN or self._opened_at is None:
            return 0.0
        elapsed = self._clock() - self._opened_at
        return max(0.0, self._cooldown_s - elapsed)


# Process singleton, mirrors llm/budget.py pattern.
_singleton: LLMCircuitBreaker | None = None
_singleton_lock = threading.Lock()


def get_circuit_breaker() -> LLMCircuitBreaker:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = LLMCircuitBreaker()
    return _singleton


def reset_circuit_breaker() -> None:
    """Test helper; replace the singleton with a fresh breaker."""
    global _singleton
    with _singleton_lock:
        _singleton = None


__all__ = (
    "BreakerSnapshot",
    "BreakerState",
    "CircuitOpen",
    "LLMCircuitBreaker",
    "get_circuit_breaker",
    "reset_circuit_breaker",
)
