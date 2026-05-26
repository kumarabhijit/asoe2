"""Dead-letter queue for terminal connector failures — PARITY-6.

Integration review requirement: a poison-message DLQ for Graph 429s
past retry, SAP OData pool exhaustion, OMS write failures post-recipe-
success. The operator dashboard consumes ``list_for_tenant(tenant_id)``
to surface pending compensations from ``orchestration/outbox.py``.

Initial implementation is in-process; a Postgres-backed variant ships
when the first real connector saturates the in-memory store.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger("asoe.api.dead_letter_queue")


@dataclass(frozen=True)
class DLQEntry:
    """One dead-lettered message."""

    source: str       # gateway name: graph, sap, oms, document_extraction, ...
    operation: str    # operation name (e.g. list_messages)
    tenant_id: str
    reason: str       # short machine-readable reason
    payload: Dict[str, Any]
    recorded_at: int  # unix seconds


_STORE: List[DLQEntry] = []
_LOCK = threading.RLock()


def reset() -> None:
    with _LOCK:
        _STORE.clear()


def record(
    *,
    source: str,
    operation: str,
    tenant_id: str,
    reason: str,
    payload: Dict[str, Any] | None = None,
) -> DLQEntry:
    if not source or not operation or not tenant_id:
        raise ValueError("dead_letter_queue.record requires source/operation/tenant_id")
    entry = DLQEntry(
        source=source,
        operation=operation,
        tenant_id=tenant_id,
        reason=reason,
        payload=dict(payload or {}),
        recorded_at=int(time.time()),
    )
    with _LOCK:
        _STORE.append(entry)
    logger.warning(
        "dead-letter source=%s op=%s tenant=%s reason=%s",
        source, operation, tenant_id, reason,
    )
    return entry


def list_for_tenant(tenant_id: str) -> List[DLQEntry]:
    with _LOCK:
        return [e for e in _STORE if e.tenant_id == tenant_id]


def count() -> int:
    with _LOCK:
        return len(_STORE)
