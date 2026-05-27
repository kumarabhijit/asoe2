"""PARITY-6 — shadow-mode runner.

Wraps a (real, stub) gateway-call pair. The real call's return is
the authoritative response the recipe consumes; the stub's return
is shadow-only, diff'd against the real output and recorded in a
compliance-reviewable in-process log.

Failure modes:

  * **Real-side error**: the runner falls back to the stub return so
    the request still completes. The diff log records the failure
    for review. This is opt-in observability — a connector that's
    failing repeatedly is caught upstream by the circuit breaker
    (``gateways/circuit_breaker.py``), not here.
  * **Stub-side error**: extremely unlikely (the stub is deterministic
    by construction). Logged and the real return is used.

The Q9 thresholds (``tests/eval/shadow_mode_thresholds.yaml``) gate
the connector flip from shadow → real-only:
  * audit-bearing fields: 0% diff vs stub.
  * derived / contextual: ≤ 5% diff.
  * 7-day window < 1% audit-bearing diff rate.

The diff log here is the substrate the threshold check consumes; a
separate offline reviewer tooling aggregates by-tenant + by-connector
+ by-day.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("asoe.gateways.shadow_mode")


@dataclass(frozen=True)
class DiffEntry:
    connector: str
    operation: str
    recorded_at: int
    audit_bearing_diffs: Set[str] = field(default_factory=set)
    derived_diffs: Set[str] = field(default_factory=set)
    real_error: Optional[str] = None
    stub_error: Optional[str] = None


_LOG: List[DiffEntry] = []
_LOCK = threading.RLock()


def get_diff_log() -> List[DiffEntry]:
    """Return the in-process diff log (mutable list for test cleanup)."""
    return _LOG


def _diff(real: Dict[str, Any], stub: Dict[str, Any], fields: Set[str]) -> Set[str]:
    """Per-field diff that distinguishes ``key: None`` from a missing
    key. ``dict.get`` collapses both to ``None``, so a real return
    of ``{"status": None}`` and a stub of ``{}`` would silently
    register as "no diff" — which is exactly the audit-bearing
    silence the Q9 acceptance check exists to prevent.
    """
    out: Set[str] = set()
    for f in fields:
        present_real = f in real
        present_stub = f in stub
        if present_real != present_stub:
            out.add(f)
            continue
        if real.get(f) != stub.get(f):
            out.add(f)
    return out


class ShadowRunner:
    """Run a real + stub gateway call in lockstep and diff the outputs.

    Construct one per (connector, operation) — the runner is cheap;
    re-use is fine but not required.

    The ``audit_bearing_fields`` / ``derived_fields`` sets are the
    contract from ``compliance/audit_bearing_registry.yaml`` —
    field-by-field they decide which bucket a diff falls into for
    the Q9 acceptance check.
    """

    def __init__(
        self,
        *,
        connector: str,
        operation: str,
        audit_bearing_fields: Optional[Set[str]] = None,
        derived_fields: Optional[Set[str]] = None,
    ) -> None:
        self.connector = connector
        self.operation = operation
        self.audit_bearing_fields = audit_bearing_fields or set()
        self.derived_fields = derived_fields or set()
        # The DiffEntry recorded by the most recent ``run()`` call.
        # Callers needing to react to the per-call live-side error must
        # read this attribute rather than ``get_diff_log()[-1]`` — a
        # concurrent connector running on another thread can append to
        # the global log between this runner's append and the caller's
        # read, racing the ``[-1]`` lookup.
        self.last_entry: Optional[DiffEntry] = None

    def run(
        self,
        *,
        real: Callable[..., Dict[str, Any]],
        stub: Callable[..., Dict[str, Any]],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        real_out: Optional[Dict[str, Any]] = None
        real_err: Optional[str] = None
        stub_out: Optional[Dict[str, Any]] = None
        stub_err: Optional[str] = None

        try:
            real_out = real(**params)
        except Exception as exc:  # noqa: BLE001 — shadow must not crash caller
            real_err = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "shadow real-side error connector=%s op=%s err=%s",
                self.connector, self.operation, real_err,
            )

        try:
            stub_out = stub(**params)
        except Exception as exc:  # noqa: BLE001
            stub_err = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "shadow stub-side error connector=%s op=%s err=%s",
                self.connector, self.operation, stub_err,
            )

        audit_diffs: Set[str] = set()
        derived_diffs: Set[str] = set()
        if real_out is not None and stub_out is not None:
            audit_diffs = _diff(real_out, stub_out, self.audit_bearing_fields)
            derived_diffs = _diff(real_out, stub_out, self.derived_fields)

        entry = DiffEntry(
            connector=self.connector,
            operation=self.operation,
            recorded_at=int(time.time()),
            audit_bearing_diffs=audit_diffs,
            derived_diffs=derived_diffs,
            real_error=real_err,
            stub_error=stub_err,
        )
        with _LOCK:
            _LOG.append(entry)
        self.last_entry = entry

        # Return contract: real wins when available; stub is the
        # safety net when real failed. If both failed, surface the
        # real error so the caller's circuit breaker trips.
        if real_out is not None:
            return real_out
        if stub_out is not None:
            return stub_out
        raise RuntimeError(
            f"shadow runner: both real and stub failed connector={self.connector} "
            f"op={self.operation} real_err={real_err} stub_err={stub_err}"
        )
