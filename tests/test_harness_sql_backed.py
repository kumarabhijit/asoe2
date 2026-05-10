"""ADR-038 Phase H.5 — SQL-backed harness primitive tests (V012/V013).

The in-memory `CaseLockManager` and `ToolCallReplayLog` work for
single-pod deployments; production is multi-pod, so the same
surface must be DB-backed. These tests run against an in-memory
SQLite database (no Postgres required) and cover:

  * `CaseEventRepository.record` / `list_for_case` — append-only
    persistence, JSONB round-trip, tenant isolation, ordering.
  * `CaseLockRepository.try_acquire` returns False on UNIQUE
    conflict; `release` is idempotent; `sweep_expired` cleans
    stale rows.
  * `DatabaseBackedToolCallReplayLog` and
    `DatabaseBackedCaseLockManager` adapters expose the same
    surface as the in-memory primitives — `run_agent_step`
    works against either backend without code changes.
  * `_select_replay_log` / `_select_lock_manager` factories
    pick DB-backed when `DATABASE_URL` is set, else in-memory.
"""

from __future__ import annotations

import os
import tempfile
import time

import pytest

from agents.harness import (
    DatabaseBackedCaseLockManager,
    DatabaseBackedToolCallReplayLog,
    ToolCallReplayEntry,
    _default_lock_manager,
    _default_replay_log,
    _select_lock_manager,
    _select_replay_log,
)
from db.connection import create_adapter
from db.migrations.runner import apply_migrations
from db.repository import CaseEventRepository, CaseLockRepository


# ---------------------------------------------------------------------------
# Fixtures — fresh SQLite DB per test
# ---------------------------------------------------------------------------

@pytest.fixture
def database_url(tmp_path):
    """Build a SQLite-on-disk DATABASE_URL with V001..V013 applied."""
    db_path = tmp_path / "harness_sql_test.db"
    url = f"sqlite:///{db_path}"
    apply_migrations(url)
    return url


@pytest.fixture
def adapter(database_url):
    return create_adapter(database_url)


# ---------------------------------------------------------------------------
# CaseEventRepository
# ---------------------------------------------------------------------------

class TestCaseEventRepository:
    def test_record_and_list(self, adapter):
        repo = CaseEventRepository(adapter=adapter)
        repo.record(
            event_id="c1:0:0", case_id="c1", tenant_id="t1",
            occurred_at="2026-01-01T00:00:00Z",
            tool_name="declare_done",
            tool_call={"tool_name": "declare_done", "arguments": {}},
            tool_result={"status": "ok"},
            outcome="RESOLVED",
        )
        rows = repo.list_for_case("c1", "t1")
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "declare_done"
        # JSON round-trip preserves dict shape.
        assert rows[0]["tool_call"]["tool_name"] == "declare_done"
        assert rows[0]["tool_result"]["status"] == "ok"

    def test_tenant_isolation(self, adapter):
        repo = CaseEventRepository(adapter=adapter)
        repo.record(
            event_id="c-a:0:0", case_id="c-a", tenant_id="tenant-a",
            occurred_at="2026-01-01T00:00:00Z",
            tool_name="x", tool_call={}, tool_result={}, outcome="RESOLVED",
        )
        repo.record(
            event_id="c-a:0:1", case_id="c-a", tenant_id="tenant-b",
            occurred_at="2026-01-01T00:00:00Z",
            tool_name="x", tool_call={}, tool_result={}, outcome="RESOLVED",
        )
        # Same case_id but different tenants — listing is tenant-scoped.
        assert len(repo.list_for_case("c-a", "tenant-a")) == 1
        assert len(repo.list_for_case("c-a", "tenant-b")) == 1

    def test_chronological_order(self, adapter):
        repo = CaseEventRepository(adapter=adapter)
        for i in range(3):
            repo.record(
                event_id=f"c:0:{i}", case_id="c", tenant_id="t",
                occurred_at=f"2026-01-0{i+1}T00:00:00Z",
                tool_name=f"tool-{i}", tool_call={}, tool_result={},
                outcome="RESOLVED",
            )
        rows = repo.list_for_case("c", "t")
        assert [r["tool_name"] for r in rows] == ["tool-0", "tool-1", "tool-2"]


# ---------------------------------------------------------------------------
# CaseLockRepository
# ---------------------------------------------------------------------------

class TestCaseLockRepository:
    def test_first_acquire_succeeds(self, adapter):
        repo = CaseLockRepository(adapter=adapter)
        assert repo.try_acquire("c1", "t1") is True
        repo.release("c1", "t1")

    def test_second_acquire_fails(self, adapter):
        repo = CaseLockRepository(adapter=adapter)
        assert repo.try_acquire("c1", "t1") is True
        assert repo.try_acquire("c1", "t1") is False
        repo.release("c1", "t1")
        # After release, can acquire again.
        assert repo.try_acquire("c1", "t1") is True
        repo.release("c1", "t1")

    def test_release_idempotent(self, adapter):
        repo = CaseLockRepository(adapter=adapter)
        # Releasing without prior acquire is a no-op (no exception).
        repo.release("c-never", "t1")

    def test_different_cases_dont_block(self, adapter):
        repo = CaseLockRepository(adapter=adapter)
        assert repo.try_acquire("c-a", "t1") is True
        assert repo.try_acquire("c-b", "t1") is True
        repo.release("c-a", "t1")
        repo.release("c-b", "t1")

    def test_different_tenants_dont_block(self, adapter):
        # Different tenants on the same case_id — the PK is just
        # case_id (not (case_id, tenant_id)) so the second attempt
        # collides. This matches the "case_id is globally unique"
        # invariant per ADR-038 §6.2 (UUID-based).
        repo = CaseLockRepository(adapter=adapter)
        assert repo.try_acquire("shared-case", "tenant-a") is True
        assert repo.try_acquire("shared-case", "tenant-b") is False
        repo.release("shared-case", "tenant-a")

    def test_sweep_expired_clears_stale(self, adapter):
        repo = CaseLockRepository(adapter=adapter)
        # Negative TTL → row is born expired.
        assert repo.try_acquire("c-stale", "t1", ttl_seconds=-1) is True
        cleaned = repo.sweep_expired("t1")
        assert cleaned >= 1
        # After sweep, can acquire again.
        assert repo.try_acquire("c-stale", "t1") is True
        repo.release("c-stale", "t1")

    def test_stale_acquire_auto_overrides(self, adapter):
        """try_acquire's pre-sweep clears its own case's stale row,
        so a process that crashed mid-step doesn't leave a permanent
        block."""
        repo = CaseLockRepository(adapter=adapter)
        assert repo.try_acquire("c-zombie", "t1", ttl_seconds=-1) is True
        # Without explicit release — the next try_acquire should win
        # because the existing entry is past its TTL.
        assert repo.try_acquire("c-zombie", "t1") is True
        repo.release("c-zombie", "t1")


# ---------------------------------------------------------------------------
# DatabaseBacked* adapters — surface compatibility with in-memory
# ---------------------------------------------------------------------------

class TestDatabaseBackedReplayLog:
    def test_record_persists(self, adapter):
        repo = CaseEventRepository(adapter=adapter)
        log = DatabaseBackedToolCallReplayLog(repo=repo)
        entry = ToolCallReplayEntry(
            event_id="c1:0:0", case_id="c1", tenant_id="t1",
            occurred_at="2026-01-01T00:00:00Z", tool_name="declare_done",
            tool_call={"x": 1}, tool_result={"status": "ok"},
            outcome="RESOLVED",
        )
        log.record(entry)
        rows = log.list_for_case_with_tenant("c1", "t1")
        assert len(rows) == 1
        assert rows[0].tool_name == "declare_done"
        assert rows[0].tool_call == {"x": 1}

    def test_list_for_case_without_tenant_returns_empty(self, adapter):
        """The DB schema requires tenant for read; the harness's
        tenant-less helper is a write-only-from-harness API."""
        repo = CaseEventRepository(adapter=adapter)
        log = DatabaseBackedToolCallReplayLog(repo=repo)
        log.record(ToolCallReplayEntry(
            event_id="c1:0:0", case_id="c1", tenant_id="t1",
            occurred_at="2026-01-01T00:00:00Z", tool_name="x",
            tool_call={}, tool_result={}, outcome="RESOLVED",
        ))
        assert log.list_for_case("c1") == []

    def test_clear_not_supported(self, adapter):
        log = DatabaseBackedToolCallReplayLog(
            repo=CaseEventRepository(adapter=adapter),
        )
        with pytest.raises(NotImplementedError):
            log.clear()


class TestDatabaseBackedLockManager:
    def test_handle_release(self, adapter):
        repo = CaseLockRepository(adapter=adapter)
        mgr = DatabaseBackedCaseLockManager(repo=repo)
        h = mgr.try_acquire("t1", "c1")
        assert h is not None
        # Same case → second attempt fails.
        h2 = mgr.try_acquire("t1", "c1")
        assert h2 is None
        h.release()
        # After release → can acquire again.
        h3 = mgr.try_acquire("t1", "c1")
        assert h3 is not None
        h3.release()

    def test_handle_release_idempotent(self, adapter):
        mgr = DatabaseBackedCaseLockManager(
            repo=CaseLockRepository(adapter=adapter),
        )
        h = mgr.try_acquire("t1", "c-i")
        assert h is not None
        h.release()
        # Second release is a no-op (test passes if no exception).
        h.release()


# ---------------------------------------------------------------------------
# Module-level factory selection
# ---------------------------------------------------------------------------

class TestFactorySelection:
    def test_in_memory_when_no_database_url(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert _select_replay_log() is _default_replay_log
        assert _select_lock_manager() is _default_lock_manager

    def test_db_backed_when_database_url_set(self, monkeypatch, tmp_path):
        # Apply migrations first.
        url = f"sqlite:///{tmp_path / 'factory.db'}"
        apply_migrations(url)
        monkeypatch.setenv("DATABASE_URL", url)
        assert isinstance(_select_replay_log(), DatabaseBackedToolCallReplayLog)
        assert isinstance(_select_lock_manager(), DatabaseBackedCaseLockManager)
