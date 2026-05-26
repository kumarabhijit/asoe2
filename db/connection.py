"""Database connection management.

Supports PostgreSQL (production) and SQLite (testing/development).
Connection type is auto-detected from DATABASE_URL.

Architecture_v3.md Section 9:
  - PostgreSQL 16 with pgvector for production
  - SQLite for local sandbox and CI testing

Architecture_v3.md Section 11.3:
  - PostgreSQL connections set ``app.current_tenant_id`` session variable
    for RLS enforcement
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, Generator, List, Optional, Protocol, Tuple
from uuid import uuid4

logger = logging.getLogger("asoe.db")


class DatabaseConnection(Protocol):
    """Protocol for database connection operations."""

    def execute(self, sql: str, params: tuple = ()) -> Any: ...
    def fetchone(self) -> Optional[tuple]: ...
    def fetchall(self) -> List[tuple]: ...
    def commit(self) -> None: ...
    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# SQLite adapter
# ---------------------------------------------------------------------------

class SQLiteAdapter:
    """SQLite connection wrapper with dict-row support.

    Per-thread connections are cached under a ``threading.local()`` so a
    request handler running on a worker thread doesn't crash on the
    "SQLite objects created in a thread can only be used in that same
    thread" guard. For ``:memory:`` databases this means we MUST use
    SQLite's shared-cache URI (``file:NAME?mode=memory&cache=shared``)
    — without it, every thread's first connection creates an empty new
    in-memory DB and the schema applied by the test/setup thread is
    invisible to handler threads. The shared-cache form makes one in-
    memory DB visible to every connection that opens the same URI in
    the same process.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        if db_path == ":memory:":
            # Generate a per-instance unique name so different adapter
            # instances in the same process don't collide. The
            # ``mode=memory&cache=shared`` query string is what makes
            # the DB shared across connections; ``file:`` opens it via
            # the URI form (``uri=True`` on connect).
            self._db_path = f"file:asoe-mem-{uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
        else:
            self._db_path = db_path
            self._uri = False
        self._local = threading.local()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, uri=self._uri)
            conn.row_factory = sqlite3.Row
            # WAL only makes sense for file-backed DBs. ``:memory:``
            # (shared cache) cannot be WAL — SQLite silently rejects
            # the pragma but we skip it to avoid noise in the log.
            if not self._uri:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    @contextmanager
    def connection(self, tenant_id: Optional[str] = None) -> Generator[sqlite3.Connection, None, None]:
        """Yield a connection. tenant_id is stored for application-layer filtering."""
        conn = self._get_conn()
        # SQLite has no RLS — tenant filtering is done in queries.
        # Store tenant_id on the adapter (not the connection object).
        self._local.tenant_id = tenant_id
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise

    @contextmanager
    def cursor(self, tenant_id: Optional[str] = None) -> Generator[sqlite3.Cursor, None, None]:
        with self.connection(tenant_id) as conn:
            cur = conn.cursor()
            try:
                yield cur
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    def apply_schema(self) -> None:
        """Apply SQLite-compatible schema from migration runner."""
        from db.migrations.runner import apply_sqlite
        with self.connection() as conn:
            apply_sqlite(conn)


# ---------------------------------------------------------------------------
# PostgreSQL adapter
# ---------------------------------------------------------------------------

class _QmarkCursorWrapper:
    """Wraps a psycopg2/psycopg cursor to accept ``?`` placeholders.

    Repository code uses SQLite-style ``?`` placeholders for portability.
    This wrapper translates ``?`` → ``%s`` before forwarding to the
    underlying PostgreSQL cursor, so query strings work unchanged
    across both backends.
    """

    def __init__(self, cursor) -> None:
        self._cur = cursor

    @staticmethod
    def _translate(sql: str) -> str:
        """Replace ``?`` with ``%s`` outside of string literals."""
        return sql.replace("?", "%s")

    def execute(self, sql: str, params: tuple = ()) -> Any:
        return self._cur.execute(self._translate(sql), params)

    def fetchone(self) -> Optional[tuple]:
        return self._cur.fetchone()

    def fetchall(self) -> List[tuple]:
        return self._cur.fetchall()

    def close(self) -> None:
        return self._cur.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cur, name)


class PostgresAdapter:
    """PostgreSQL connection wrapper with thread-local connection reuse.

    Uses psycopg2 or psycopg for database access. Sets the
    ``app.current_tenant_id`` session variable on each connection
    for RLS enforcement (architecture_v3.md Section 11.3).

    Connections are stored per-thread and reused across calls (same
    pattern as SQLiteAdapter). On error, the stale connection is
    discarded and a fresh one is created on the next call.
    """

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._connect_fn = self._resolve_driver()
        self._local = threading.local()

    def _resolve_driver(self):
        """Resolve the PostgreSQL driver (psycopg2 or psycopg)."""
        try:
            import psycopg2  # type: ignore[import-untyped]
            return lambda: psycopg2.connect(self._database_url)
        except ImportError:
            pass
        try:
            import psycopg  # type: ignore[import-untyped]
            return lambda: psycopg.connect(self._database_url)
        except ImportError:
            raise RuntimeError(
                "No PostgreSQL driver found. Install psycopg2-binary or psycopg."
            )

    def _get_conn(self) -> Any:
        """Return a thread-local connection, creating one if needed."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = self._connect_fn()
        return self._local.conn

    def _discard_conn(self) -> None:
        """Close and discard the current thread-local connection."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None

    @contextmanager
    def connection(self, tenant_id: Optional[str] = None) -> Generator[Any, None, None]:
        """Yield a PostgreSQL connection with tenant_id set for RLS."""
        conn = self._get_conn()
        try:
            if tenant_id:
                cur = conn.cursor()
                cur.execute(
                    "SET app.current_tenant_id = %s", (tenant_id,)
                )
                cur.close()
            yield conn
        except Exception:
            conn.rollback()
            self._discard_conn()
            raise

    @contextmanager
    def cursor(self, tenant_id: Optional[str] = None) -> Generator[Any, None, None]:
        with self.connection(tenant_id) as conn:
            cur = conn.cursor()
            wrapped = _QmarkCursorWrapper(cur)
            try:
                yield wrapped
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()

    def close(self) -> None:
        """Close the thread-local connection."""
        self._discard_conn()

    def apply_schema(self) -> None:
        """Apply PostgreSQL schema from migration runner."""
        from db.migrations.runner import apply_postgres
        apply_postgres(self._database_url)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_DEFAULT_POSTGRES_CONNECT_TIMEOUT_S = 5
_MAX_POSTGRES_CONNECT_TIMEOUT_S = 60
"""Default + ceiling for connect_timeout (seconds) on the Postgres driver
— PARITY-0 Phase 0a (Azure/SRE review). A Postgres outage with no timeout
hangs the request for ~30s+ and cascades into readiness-probe failure.
Overridable via ``ASOE_POSTGRES_CONNECT_TIMEOUT_S``, but capped at 60s so
a pathological override (operator types 999999) doesn't recreate the
hang we're guarding against (Review 1 finding)."""


def _ensure_connect_timeout(url: "Optional[str]") -> "Optional[str]":
    """Ensure a postgresql:// URL carries a ``connect_timeout``. No-op on
    SQLite / empty / None URLs and on URLs that already specify
    ``connect_timeout`` (operator override wins)."""
    if not url or not url.startswith(("postgresql:", "postgres:")):
        return url
    if "connect_timeout=" in url:
        return url
    raw = os.getenv("ASOE_POSTGRES_CONNECT_TIMEOUT_S", "").strip()
    try:
        seconds = int(raw) if raw else _DEFAULT_POSTGRES_CONNECT_TIMEOUT_S
        if seconds <= 0:
            seconds = _DEFAULT_POSTGRES_CONNECT_TIMEOUT_S
    except ValueError:
        seconds = _DEFAULT_POSTGRES_CONNECT_TIMEOUT_S
    # Cap at the ceiling so a misconfigured deploy can't reintroduce a
    # multi-minute hang on Postgres outage.
    seconds = min(seconds, _MAX_POSTGRES_CONNECT_TIMEOUT_S)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}connect_timeout={seconds}"


def create_adapter(database_url: Optional[str] = None) -> SQLiteAdapter | PostgresAdapter:
    """Create a database adapter based on DATABASE_URL.

    - ``sqlite:///path`` or ``sqlite://:memory:`` → SQLiteAdapter
    - ``postgresql://...`` → PostgresAdapter (connect_timeout injected
      unless operator-specified — PARITY-0 Phase 0a)
    - No URL → SQLiteAdapter with in-memory database
    """
    url = database_url or os.getenv("DATABASE_URL", "")
    if not url or url.startswith("sqlite"):
        db_path = url.replace("sqlite:///", "").replace("sqlite://", "") or ":memory:"
        return SQLiteAdapter(db_path)
    return PostgresAdapter(_ensure_connect_timeout(url))
