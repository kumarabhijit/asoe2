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
# This module is the durable LEDGER + the compensation QUEUE: every effect
# attempt is recorded with its outcome; failures are flagged `needs_compensation`
# and surfaced via `pending_compensation()` until `mark_compensated()` clears
# them. The automatic reconciliation WORKER (retry/undo loop) is a separate
# productionization step — this is the substrate it runs on.
#
# Process-local (parity with the in-memory audit log / gateway metering); a
# DB-backed outbox table is the production follow-on. Reset per test.

import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4


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
    compensated: bool = False
    compensated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


_lock = threading.Lock()
_entries: List[OutboxEntry] = []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
) -> OutboxEntry:
    """Record one effect outcome. SUCCESS → committed; anything else →
    needs_compensation (the partial-failure window the outbox closes)."""
    committed = status == "SUCCESS"
    entry = OutboxEntry(
        id=str(uuid4()),
        tenant_id=tenant_id,
        trace_id=trace_id,
        recipe=recipe,
        gateway=gateway,
        operation=operation,
        status=status,
        recipe_status=recipe_status,
        committed=committed,
        needs_compensation=not committed,
        error=error,
        created_at=_now(),
    )
    with _lock:
        _entries.append(entry)
    return entry


def pending_compensation(tenant_id: Optional[str] = None) -> List[OutboxEntry]:
    """Effects that failed and have not yet been compensated — the work queue
    a reconciler drains."""
    with _lock:
        return [
            e for e in _entries
            if e.needs_compensation and not e.compensated
            and (tenant_id is None or e.tenant_id == tenant_id)
        ]


def mark_compensated(entry_id: str) -> bool:
    """Mark a pending entry as compensated (the reconciler succeeded). Returns
    False if the id is unknown."""
    with _lock:
        for e in _entries:
            if e.id == entry_id:
                e.compensated = True
                e.compensated_at = _now()
                return True
    return False


def all_entries(tenant_id: Optional[str] = None) -> List[OutboxEntry]:
    with _lock:
        return [
            e for e in _entries
            if tenant_id is None or e.tenant_id == tenant_id
        ]


def reset() -> None:
    """Test helper — drop the ledger."""
    with _lock:
        _entries.clear()
