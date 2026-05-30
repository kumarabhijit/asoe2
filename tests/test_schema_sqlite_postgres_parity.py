"""Lock: the hand-maintained SQLite mirror matches the Postgres `.sql` chain.

`db/migrations/runner.py::apply_sqlite` is a hand-written mirror of the
`V0*.sql` files applied by `apply_postgres`. The review found this mirror
had already silently drifted (V010 `case_correlation_keys` and V014
`order_case.updated_at` were missing from the SQLite path), which made the
DB-backed CaseStore untestable. Nothing caught it.

This test applies BOTH backends and asserts table + column parity, so any
future column added to a `.sql` migration but forgotten in the SQLite
mirror (or vice versa) fails CI. Runs against real Postgres in the
`pytest-postgres` job; skipped when ASOE_TEST_POSTGRES_URL is unset.

Allowlisted differences (documented, not drift):
  * exceptions.context_embedding — pgvector VECTOR(1536) (V001); SQLite
    has no vector type, so the mirror omits it by design.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from db.migrations.runner import apply_postgres, apply_sqlite

# (table, column) pairs that legitimately exist only on Postgres.
_PG_ONLY_COLUMNS: set[tuple[str, str]] = {
    ("exceptions", "context_embedding"),
}


def _pg_url() -> str | None:
    return os.getenv("ASOE_TEST_POSTGRES_URL")


pytestmark = pytest.mark.skipif(
    not _pg_url(),
    reason="schema parity requires Postgres (set ASOE_TEST_POSTGRES_URL)",
)


def _sqlite_schema() -> dict[str, set[str]]:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_sqlite(conn)
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    out = {
        t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
        for t in tables
    }
    conn.close()
    return out


def _postgres_schema(url: str) -> dict[str, set[str]]:
    import psycopg2

    # Fresh throwaway DB so a prior chain doesn't mask a missing migration.
    base = url.rsplit("/", 1)[0]
    admin = psycopg2.connect(f"{base}/postgres")
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute("DROP DATABASE IF EXISTS schema_parity_check")
    cur.execute("CREATE DATABASE schema_parity_check")
    admin.close()

    target = f"{base}/schema_parity_check"
    apply_postgres(target)
    conn = psycopg2.connect(target)
    cur = conn.cursor()
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public'"
    )
    tables = {r[0] for r in cur.fetchall()}
    out: dict[str, set[str]] = {}
    for t in tables:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s",
            (t,),
        )
        out[t] = {r[0] for r in cur.fetchall()}
    conn.close()
    return out


@pytest.fixture(scope="module")
def schemas():
    return _sqlite_schema(), _postgres_schema(_pg_url())


def test_table_sets_match(schemas):
    sqlite_s, pg_s = schemas
    # schema_migrations + every domain table must exist on both backends.
    sqlite_only = set(sqlite_s) - set(pg_s)
    pg_only = set(pg_s) - set(sqlite_s)
    assert not sqlite_only, f"tables only in SQLite mirror: {sorted(sqlite_only)}"
    assert not pg_only, f"tables only in Postgres .sql chain: {sorted(pg_only)}"


def test_columns_match_per_table(schemas):
    sqlite_s, pg_s = schemas
    problems: list[str] = []
    for table in sorted(set(sqlite_s) & set(pg_s)):
        sc, pc = sqlite_s[table], pg_s[table]
        missing_in_sqlite = {
            c for c in (pc - sc) if (table, c) not in _PG_ONLY_COLUMNS
        }
        extra_in_sqlite = sc - pc
        if missing_in_sqlite:
            problems.append(
                f"{table}: columns in .sql but MISSING from SQLite mirror: "
                f"{sorted(missing_in_sqlite)}"
            )
        if extra_in_sqlite:
            problems.append(
                f"{table}: columns in SQLite mirror but not in .sql: "
                f"{sorted(extra_in_sqlite)}"
            )
    assert not problems, "SQLite mirror drifted from the Postgres chain:\n" + "\n".join(problems)
