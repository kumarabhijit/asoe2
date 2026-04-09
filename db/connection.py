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
    """SQLite connection wrapper with dict-row support."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._local = threading.local()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
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

class PostgresAdapter:
    """PostgreSQL connection wrapper.

    Uses psycopg2 or psycopg for database access. Sets the
    ``app.current_tenant_id`` session variable on each connection
    for RLS enforcement (architecture_v3.md Section 11.3).
    """

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._connect_fn = self._resolve_driver()

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

    @contextmanager
    def connection(self, tenant_id: Optional[str] = None) -> Generator[Any, None, None]:
        """Yield a PostgreSQL connection with tenant_id set for RLS."""
        conn = self._connect_fn()
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
            raise
        finally:
            conn.close()

    @contextmanager
    def cursor(self, tenant_id: Optional[str] = None) -> Generator[Any, None, None]:
        with self.connection(tenant_id) as conn:
            cur = conn.cursor()
            try:
                yield cur
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()

    def close(self) -> None:
        pass  # Connections are closed per-request

    def apply_schema(self) -> None:
        """Apply PostgreSQL schema from migration runner."""
        from db.migrations.runner import apply_postgres
        apply_postgres(self._database_url)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_adapter(database_url: Optional[str] = None) -> SQLiteAdapter | PostgresAdapter:
    """Create a database adapter based on DATABASE_URL.

    - ``sqlite:///path`` or ``sqlite://:memory:`` → SQLiteAdapter
    - ``postgresql://...`` → PostgresAdapter
    - No URL → SQLiteAdapter with in-memory database
    """
    url = database_url or os.getenv("DATABASE_URL", "")
    if not url or url.startswith("sqlite"):
        db_path = url.replace("sqlite:///", "").replace("sqlite://", "") or ":memory:"
        return SQLiteAdapter(db_path)
    return PostgresAdapter(url)
