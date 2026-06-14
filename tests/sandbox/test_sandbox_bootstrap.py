"""Tests for the sandbox case bootstrap (api/sandbox_bootstrap.py).

Locks the deployment-parity contract: a freshly-started sandbox backend
materialises the catalog's EDI scenarios into real cases by running them
through the graph (no LLM — deterministic fallback classifier), and does
so idempotently. Verifies the activation gate as well.
"""
from __future__ import annotations

import yaml

from api.sandbox_bootstrap import (
    DEFAULT_TENANT,
    _CATALOG_PATH,
    bootstrap_sandbox_cases,
    is_enabled,
)
from api.store import exception_store


def _catalog_scenario_count() -> int:
    catalog = yaml.safe_load(_CATALOG_PATH.read_text(encoding="utf-8")) or {}
    return len(catalog.get("scenarios") or [])


def _list_all(tenant_id: str):
    """Drain the paginated store for a tenant."""
    out = []
    cursor = None
    while True:
        page, cursor, has_more = exception_store.list(
            tenant_id=tenant_id, limit=200, cursor=cursor,
        )
        out.extend(page)
        if not has_more:
            break
    return out


class TestBootstrapMaterialisation:
    def test_creates_a_case_per_edi_scenario(self):
        exception_store.clear()
        expected = _catalog_scenario_count()
        assert expected > 0, "catalog carries no EDI scenarios to materialise"

        created = bootstrap_sandbox_cases(force=True)

        # Every catalog EDI scenario must run the graph to a persisted record.
        assert created == expected
        assert len(_list_all(DEFAULT_TENANT)) == expected

    def test_persisted_records_carry_graph_output(self):
        exception_store.clear()
        bootstrap_sandbox_cases(force=True)

        records = _list_all(DEFAULT_TENANT)
        # Cases are "really created" — they carry the graph's classification
        # and Compliance Shadow verdict, not hand-stamped values.
        assert all(r.intent for r in records)
        assert all(r.shadow_verdict for r in records)

    def test_idempotent_when_store_already_populated(self):
        exception_store.clear()
        first = bootstrap_sandbox_cases(force=True)
        assert first > 0

        # A second pass without force must be a no-op (Azure replica restart).
        again = bootstrap_sandbox_cases()
        assert again == 0
        assert len(_list_all(DEFAULT_TENANT)) == first

    def test_missing_catalog_is_a_safe_noop(self, tmp_path):
        exception_store.clear()
        created = bootstrap_sandbox_cases(
            catalog_path=tmp_path / "does-not-exist.yaml", force=True,
        )
        assert created == 0


class TestActivationGate:
    def test_enabled_only_in_sandbox_with_flag(self, monkeypatch):
        monkeypatch.setenv("ASOE_ENV", "sandbox")
        monkeypatch.setenv("ASOE_SANDBOX_BOOTSTRAP", "1")
        assert is_enabled() is True

    def test_disabled_without_flag(self, monkeypatch):
        monkeypatch.setenv("ASOE_ENV", "sandbox")
        monkeypatch.delenv("ASOE_SANDBOX_BOOTSTRAP", raising=False)
        assert is_enabled() is False

    def test_disabled_outside_sandbox(self, monkeypatch):
        monkeypatch.setenv("ASOE_ENV", "production")
        monkeypatch.setenv("ASOE_SANDBOX_BOOTSTRAP", "1")
        assert is_enabled() is False
