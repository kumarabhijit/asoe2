"""Process-wide database adapter singleton.

Provides ``get_shared_adapter()`` — a single ``SQLiteAdapter`` /
``PostgresAdapter`` instance reused across consumers (route handlers,
gateway adapters, etc.) so they all see the same database.

Why a singleton: ``SQLiteAdapter(":memory:")`` creates a fresh database
per instance — two adapters constructed independently in the same
process would point at two different in-memory DBs and never see each
other's writes. The singleton ensures a single shared database
regardless of who constructs the consumer.

For Postgres / file-backed SQLite, multiple adapters connect to the
same persistent store, so the singleton is functionally equivalent
to non-singleton usage but still useful for connection-pool reuse.

Schema is applied on first access (idempotent — CREATE…IF NOT EXISTS).

Tests override via ``set_shared_adapter(test_adapter)`` so each test
case can swap in a freshly migrated in-memory adapter.
"""

from __future__ import annotations

import threading
from typing import Optional, Union

from db.connection import (
    PostgresAdapter,
    SQLiteAdapter,
    create_adapter,
)


_LOCK = threading.Lock()
_ADAPTER: Optional[Union[SQLiteAdapter, PostgresAdapter]] = None


def get_shared_adapter() -> Union[SQLiteAdapter, PostgresAdapter]:
    """Return the process-wide adapter, creating + migrating on first call.

    Thread-safe: subsequent callers see the already-initialised
    adapter without acquiring the lock.
    """
    global _ADAPTER
    if _ADAPTER is not None:
        return _ADAPTER
    with _LOCK:
        if _ADAPTER is None:
            adapter = create_adapter()
            adapter.apply_schema()
            _ADAPTER = adapter
    return _ADAPTER


def set_shared_adapter(adapter: Union[SQLiteAdapter, PostgresAdapter]) -> None:
    """Test-only: replace the shared adapter.

    Production code should never call this — the migration runner +
    create_adapter() resolve the right backend at startup. Tests use
    it to inject a freshly migrated in-memory adapter per test case.
    """
    global _ADAPTER
    with _LOCK:
        _ADAPTER = adapter


def reset_shared_adapter() -> None:
    """Test-only: clear the shared adapter so the next ``get_shared_adapter()``
    re-initialises from env."""
    global _ADAPTER
    with _LOCK:
        _ADAPTER = None
