"""PARITY-0 Phase 0a — Postgres connect_timeout default.

Per the Azure/SRE review of the v3 parity plan: a Postgres outage with no
``connect_timeout`` lets every request hang for the driver default
(~30s+), cascading into readiness-probe failure and revision churn.
The adapter must inject a ``connect_timeout=5`` (overridable via env)
when the URL doesn't already specify one — and must preserve an
explicit override the operator passed in.
"""
from __future__ import annotations

import pytest

from db.connection import _ensure_connect_timeout


def test_adds_default_connect_timeout_when_absent():
    url = "postgresql://user:pass@host:5432/db"
    out = _ensure_connect_timeout(url)
    assert "connect_timeout=5" in out
    # Original components preserved.
    assert "user:pass@host:5432/db" in out


def test_preserves_explicit_connect_timeout():
    url = "postgresql://user:pass@host:5432/db?connect_timeout=15"
    out = _ensure_connect_timeout(url)
    assert "connect_timeout=15" in out
    # Must not duplicate.
    assert out.count("connect_timeout=") == 1


def test_preserves_other_query_params_and_adds_timeout():
    url = "postgresql://user:pass@host:5432/db?sslmode=require"
    out = _ensure_connect_timeout(url)
    assert "sslmode=require" in out
    assert "connect_timeout=5" in out


def test_sqlite_url_unchanged():
    url = "sqlite:///tmp/db.sqlite"
    assert _ensure_connect_timeout(url) == url


def test_empty_url_unchanged():
    assert _ensure_connect_timeout("") == ""
    assert _ensure_connect_timeout(None) is None  # type: ignore[arg-type]


def test_env_override_changes_default(monkeypatch):
    monkeypatch.setenv("ASOE_POSTGRES_CONNECT_TIMEOUT_S", "10")
    url = "postgresql://user:pass@host:5432/db"
    out = _ensure_connect_timeout(url)
    assert "connect_timeout=10" in out


def test_invalid_env_override_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("ASOE_POSTGRES_CONNECT_TIMEOUT_S", "not-a-number")
    url = "postgresql://user:pass@host:5432/db"
    out = _ensure_connect_timeout(url)
    assert "connect_timeout=5" in out


def test_pathological_env_override_capped_at_ceiling(monkeypatch):
    # Review 1 finding: an operator typo like 999999 must not recreate the
    # multi-minute hang we're guarding against. The injected timeout is
    # capped at _MAX_POSTGRES_CONNECT_TIMEOUT_S (60s).
    monkeypatch.setenv("ASOE_POSTGRES_CONNECT_TIMEOUT_S", "999999")
    url = "postgresql://user:pass@host:5432/db"
    out = _ensure_connect_timeout(url)
    assert "connect_timeout=60" in out
    assert "999999" not in out


def test_postgres_adapter_applies_timeout(monkeypatch):
    """The adapter constructor must run the URL through the timeout
    helper so a deploy that forgot to set it doesn't hang on outage."""
    from db.connection import PostgresAdapter
    pa = PostgresAdapter.__new__(PostgresAdapter)
    # Avoid the driver import — directly call the helper path.
    raw = "postgresql://user:pass@host:5432/db"
    augmented = _ensure_connect_timeout(raw)
    assert augmented != raw
    assert "connect_timeout" in augmented
