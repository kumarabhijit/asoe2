"""Lock: the store singletons branch on DATABASE_URL.

The Phase H.7 deliverable is that `case_store` goes DB-backed when
DATABASE_URL is set (Azure) and stays in-memory otherwise (dev/vitest),
mirroring `exception_store`. The review found this branch had no test —
a regression that broke the case_store factory (the exact feature) would
pass CI. This locks both factories.
"""

from __future__ import annotations

from api.store import (
    CaseStore,
    DatabaseBackedCaseStore,
    DatabaseBackedStore,
    ExceptionStore,
    _create_case_store,
    _create_store,
)


def test_factories_in_memory_when_database_url_unset(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert isinstance(_create_store(), ExceptionStore)
    assert isinstance(_create_case_store(), CaseStore)


def test_factories_db_backed_when_database_url_set(monkeypatch, tmp_path):
    # A sqlite URL exercises the DB-backed branch without needing Postgres.
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/factory.db")
    assert isinstance(_create_store(), DatabaseBackedStore)
    assert isinstance(_create_case_store(), DatabaseBackedCaseStore)


def test_empty_database_url_is_treated_as_unset(monkeypatch):
    # A blank string must NOT select the DB-backed store (would point at an
    # invalid URL and fail at boot) — both factories treat "" as in-memory.
    monkeypatch.setenv("DATABASE_URL", "")
    assert isinstance(_create_store(), ExceptionStore)
    assert isinstance(_create_case_store(), CaseStore)
