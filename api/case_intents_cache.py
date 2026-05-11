"""Per-case child-intents cache (Phase 28.5.x §D2).

Maintains an in-process ``case_id -> frozenset[intent]`` view so the
``GET /api/v1/cases`` filter chip "show cases with at least one
child carrying intent X" runs in O(1) per case instead of walking
``exception_store.list_by_case`` on every request.

Invariants:

  * The cache is populated lazily on first read. The read path
    (``intents_for(case_id)``) consults ``exception_store.list_by_case``
    when a key is missing and stores the result; subsequent reads are
    O(1).
  * The cache is invalidated on ``publish_case_open`` /
    ``publish_case_update`` (already emitted on every case-state
    mutation per PR #136). The invalidator is wired by
    ``api/case_events.py`` so the cache is consistent without the
    caller having to remember to flush.
  * Thread-safe: a single module-level Lock guards reads + writes.
    Contention is low (cache hits are lock-free path acquires; only
    cold lookups and invalidations contend).

Per CLAUDE.md guardrails: the cache is a transport-layer
optimisation. It never invents intent values — it reads them
straight from the persisted ``ExceptionRecord.intent`` field via
the existing store. The OrderCase model is unchanged
(``extra="forbid"``); the cache feeds the **response dict** in
``api/routes/cases.py``.
"""
from __future__ import annotations

import threading
from typing import Dict, FrozenSet, Optional

from api.store import exception_store


_lock = threading.Lock()
# Cache key is ``(tenant_id, case_id)`` so cross-tenant cases never
# share an entry even when a tenant accidentally reuses a UUID.
_cache: Dict[tuple[str, str], FrozenSet[str]] = {}


def intents_for(tenant_id: str, case_id: str) -> FrozenSet[str]:
    """Return the deduped set of intents across the case's children.

    Lazy: a miss reads ``exception_store.list_by_case`` and populates
    the entry. Returns an empty frozenset for cases with no children
    yet (just-opened Manual Orders), which the UI maps to "no intent
    filter matches."
    """
    key = (tenant_id, case_id)
    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            return cached
    # Read outside the lock — list_by_case acquires its own store lock
    # and we don't want to nest. We re-acquire below to write.
    records = exception_store.list_by_case(tenant_id, case_id)
    intents = frozenset(
        r.intent for r in records if isinstance(r.intent, str) and r.intent
    )
    with _lock:
        _cache[key] = intents
    return intents


def invalidate(tenant_id: str, case_id: str) -> None:
    """Drop the cached entry for a case so the next read re-derives
    from the exception store. Called by ``api/case_events.py`` on
    ``publish_case_open`` / ``publish_case_update`` — both of which
    fire on every materialise + every status mutation, so the cache
    is consistent within one event-loop tick of the underlying
    persistence change.
    """
    key = (tenant_id, case_id)
    with _lock:
        _cache.pop(key, None)


def clear() -> None:
    """Test helper — drop all cached entries."""
    with _lock:
        _cache.clear()


def has_intent(tenant_id: str, case_id: str, intent: str) -> bool:
    """Convenience predicate for the filter-chip path."""
    return intent in intents_for(tenant_id, case_id)


def matches_any(
    tenant_id: str, case_id: str, intents: Optional[FrozenSet[str]],
) -> bool:
    """Filter helper: ``intents=None`` or empty -> always matches
    (no filter applied); otherwise return True when the case has at
    least one child whose intent is in the requested set.
    """
    if not intents:
        return True
    case_intents = intents_for(tenant_id, case_id)
    return bool(case_intents & intents)
