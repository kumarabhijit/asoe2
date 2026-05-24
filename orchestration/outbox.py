from __future__ import annotations

# Effect outbox + compensation queue (DoR #6).
#
# `apply_effects` fires a recipe's GatewayEffects (the ERP sales-order write, the
# buyer-notification send, …) AFTER the recipe result is already committed. That
# leaves two partial-failure windows the outbox pattern exists to close:
#
#   * ERP-submit-OK — the external ERP write SUCCEEDED. We record it durably so
#     that even if downstream local persistence fails, the committed external
#     effect is known and reconcilable (never silently lost).
#   * reply-fail — the buyer-notification send FAILED after the recipe reported
#     READY_TO_SEND. We record it as needing compensation so a reconciler can
#     retry / escalate instead of leaving a half-sent state.
#
# This module is the durable LEDGER + the compensation QUEUE + the reconciler.
# Storage is pluggable behind `_Backend`: the in-memory backend is the default
# (process-local, parity with the in-memory audit log); when `DATABASE_URL` is
# set the DB backend persists to the `effect_outbox` table so the queue survives
# restarts (the same selection rule as `api.store`). The public functions are
# backend-agnostic, so callers (`apply_effects`, the reconcile endpoint, the
# scheduler, tests) are unchanged.

import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger("asoe.outbox")


@dataclass
class OutboxEntry:
    """One recorded gateway-effect attempt."""

    id: str
    tenant_id: str
    trace_id: Optional[str]
    recipe: Optional[str]
    gateway: str
    operation: str
    status: str                 # the GatewayResponse status (SUCCESS/FAILED/…)
    recipe_status: Optional[str]
    committed: bool             # the external side effect succeeded
    needs_compensation: bool    # failed → a reconciler must retry / undo / escalate
    error: Optional[str]
    created_at: str
    # The originating gateway-call params, retained so the reconciler can RETRY
    # the effect idempotently (delivery idempotency on the send side dedupes a
    # duplicate retry — DoR #5).
    params: dict = field(default_factory=dict)
    attempts: int = 0           # reconciliation retries performed
    escalated: bool = False     # retries exhausted → human follow-up required
    compensated: bool = False
    compensated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class _InMemoryBackend:
    """Process-local ledger (default; the original behaviour)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: List[OutboxEntry] = []

    def record(self, entry: OutboxEntry) -> None:
        with self._lock:
            self._entries.append(entry)

    def pending(self, tenant_id: Optional[str]) -> List[OutboxEntry]:
        with self._lock:
            return [
                e for e in self._entries
                if e.needs_compensation and not e.compensated and not e.escalated
                and (tenant_id is None or e.tenant_id == tenant_id)
            ]

    def all(self, tenant_id: Optional[str]) -> List[OutboxEntry]:
        with self._lock:
            return [
                e for e in self._entries
                if tenant_id is None or e.tenant_id == tenant_id
            ]

    def _find(self, entry_id: str) -> Optional[OutboxEntry]:
        for e in self._entries:
            if e.id == entry_id:
                return e
        return None

    def increment_attempts(self, tenant_id: str, entry_id: str) -> int:
        with self._lock:
            e = self._find(entry_id)
            if e is None:
                return 0
            e.attempts += 1
            return e.attempts

    def mark_compensated(self, tenant_id: str, entry_id: str) -> bool:
        with self._lock:
            e = self._find(entry_id)
            if e is None:
                return False
            e.compensated = True
            e.compensated_at = _now()
            return True

    def mark_escalated(self, tenant_id: str, entry_id: str) -> bool:
        with self._lock:
            e = self._find(entry_id)
            if e is None:
                return False
            e.escalated = True
            return True

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class _DbBackend:
    """Durable ledger backed by the `effect_outbox` table (DATABASE_URL set)."""

    def __init__(self, repo) -> None:
        self._repo = repo

    @staticmethod
    def _to_entry(row: Dict) -> OutboxEntry:
        return OutboxEntry(
            id=row["id"], tenant_id=row["tenant_id"], trace_id=row.get("trace_id"),
            recipe=row.get("recipe"), gateway=row["gateway"],
            operation=row["operation"], status=row["status"],
            recipe_status=row.get("recipe_status"),
            committed=bool(row.get("committed")),
            needs_compensation=bool(row.get("needs_compensation")),
            error=row.get("error"), created_at=row["created_at"],
            params=row.get("params") or {}, attempts=int(row.get("attempts", 0)),
            escalated=bool(row.get("escalated")),
            compensated=bool(row.get("compensated")),
            compensated_at=row.get("compensated_at"),
        )

    def record(self, entry: OutboxEntry) -> None:
        self._repo.insert(entry.tenant_id, entry.to_dict())

    def pending(self, tenant_id: Optional[str]) -> List[OutboxEntry]:
        if tenant_id is None:
            return []  # DB queries are tenant-scoped; the reconciler passes one
        return [self._to_entry(r) for r in self._repo.list_pending(tenant_id)]

    def all(self, tenant_id: Optional[str]) -> List[OutboxEntry]:
        if tenant_id is None:
            return []
        return [self._to_entry(r) for r in self._repo.list_all(tenant_id)]

    def increment_attempts(self, tenant_id: str, entry_id: str) -> int:
        return self._repo.increment_attempts(tenant_id, entry_id)

    def mark_compensated(self, tenant_id: str, entry_id: str) -> bool:
        return self._repo.mark_compensated(tenant_id, entry_id, _now())

    def mark_escalated(self, tenant_id: str, entry_id: str) -> bool:
        return self._repo.mark_escalated(tenant_id, entry_id)

    def clear(self) -> None:
        # Durable ledger — `reset()` is a no-op for the DB backend. Tests use a
        # fresh per-case adapter, so there is nothing to clear in-process.
        pass


def _select_backend():
    if os.getenv("DATABASE_URL", ""):
        try:
            from db.repository import OutboxRepository
            from db.shared import get_shared_adapter
            return _DbBackend(OutboxRepository(get_shared_adapter()))
        except Exception:  # pragma: no cover - fall back rather than crash boot
            logger.exception("outbox DB backend init failed; using in-memory")
    return _InMemoryBackend()


_backend = _select_backend()


def configure_backend(backend) -> None:
    """Inject a backend (tests / explicit DB wiring)."""
    global _backend
    _backend = backend


def db_backend(repo) -> "_DbBackend":
    """Build a DB backend from an OutboxRepository (for explicit wiring/tests)."""
    return _DbBackend(repo)


# ---------------------------------------------------------------------------
# Public API (backend-agnostic)
# ---------------------------------------------------------------------------

def record_effect(
    *,
    tenant_id: str,
    gateway: str,
    operation: str,
    status: str,
    recipe: Optional[str] = None,
    recipe_status: Optional[str] = None,
    trace_id: Optional[str] = None,
    error: Optional[str] = None,
    params: Optional[dict] = None,
) -> OutboxEntry:
    """Record one effect outcome. SUCCESS → committed; anything else →
    needs_compensation (the partial-failure window the outbox closes)."""
    committed = status == "SUCCESS"
    entry = OutboxEntry(
        id=str(uuid4()), tenant_id=tenant_id, trace_id=trace_id, recipe=recipe,
        gateway=gateway, operation=operation, status=status,
        recipe_status=recipe_status, committed=committed,
        needs_compensation=not committed, error=error,
        params=dict(params or {}), created_at=_now(),
    )
    _backend.record(entry)
    return entry


def pending_compensation(tenant_id: Optional[str] = None) -> List[OutboxEntry]:
    """Effects that failed and have not yet been compensated or escalated — the
    work queue a reconciler drains."""
    return _backend.pending(tenant_id)


def all_entries(tenant_id: Optional[str] = None) -> List[OutboxEntry]:
    return _backend.all(tenant_id)


def mark_compensated(entry_id: str, tenant_id: str) -> bool:
    """Mark a pending entry as compensated. False if the id is unknown."""
    return _backend.mark_compensated(tenant_id, entry_id)


def mark_escalated(entry_id: str, tenant_id: str) -> bool:
    return _backend.mark_escalated(tenant_id, entry_id)


def reconcile_pending(
    *,
    tenant_id: Optional[str] = None,
    max_attempts: int = 3,
    executor=None,
) -> dict:
    """Drain the compensation queue (DoR #6 — the reconciliation worker).

    For each pending entry it RE-RUNS the original gateway effect via the
    GatewayExecutor (retry is safe — the send path is delivery-idempotent,
    DoR #5; an already-committed ERP write is deduped downstream). On SUCCESS the
    entry is marked compensated; on failure its attempt count rises and, once it
    reaches `max_attempts`, it is escalated for human follow-up (and leaves the
    active queue). Returns a summary report. Pure of scheduling — call it from
    the scheduler or the admin trigger endpoint.
    """
    from contracts.models import GatewayRequest  # local import avoids a cycle

    if executor is None:
        from gateways.executor import GatewayExecutor
        executor = GatewayExecutor()

    report = {"retried": 0, "compensated": 0, "escalated": 0, "still_pending": 0}
    for entry in _backend.pending(tenant_id):
        report["retried"] += 1
        attempts = _backend.increment_attempts(entry.tenant_id, entry.id)
        try:
            response = executor.run(GatewayRequest(
                gateway_name=entry.gateway,
                operation=entry.operation,
                params=dict(entry.params or {}),
                trace_id=entry.trace_id or "outbox-reconcile",
            ))
            ok = response.status == "SUCCESS"
        except Exception:  # pragma: no cover - reconciler must not crash the loop
            ok = False
        if ok:
            _backend.mark_compensated(entry.tenant_id, entry.id)
            report["compensated"] += 1
        elif attempts >= max_attempts:
            _backend.mark_escalated(entry.tenant_id, entry.id)
            report["escalated"] += 1
        else:
            report["still_pending"] += 1
    return report


def reset() -> None:
    """Test helper — drop the ledger (current backend)."""
    _backend.clear()
