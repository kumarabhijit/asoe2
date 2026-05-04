"""Unit tests for db.repository.TenantConfigRepository — V006 layer."""

import pytest

from db.connection import SQLiteAdapter
from db.repository import TenantConfigRepository, _scope_hash


@pytest.fixture
def adapter() -> SQLiteAdapter:
    a = SQLiteAdapter(":memory:")
    a.apply_schema()
    return a


@pytest.fixture
def repo(adapter: SQLiteAdapter) -> TenantConfigRepository:
    return TenantConfigRepository(adapter=adapter)


def test_upsert_inserts_new_row(repo: TenantConfigRepository) -> None:
    row = repo.upsert(
        tenant_id="t1", layer="tenant", scope={},
        weights={"po_number": 0.4}, created_by="alice",
    )
    assert row["id"]
    assert row["weights"] == {"po_number": 0.4}
    assert row["scope_hash"] == _scope_hash({})


def test_upsert_overwrites_existing_scope(repo: TenantConfigRepository) -> None:
    first = repo.upsert(
        tenant_id="t1", layer="tenant", scope={},
        weights={"po_number": 0.4}, created_by="alice",
    )
    second = repo.upsert(
        tenant_id="t1", layer="tenant", scope={},
        weights={"po_number": 0.5}, created_by="bob",
    )
    assert first["id"] == second["id"]  # same row, updated in place
    assert second["weights"] == {"po_number": 0.5}


def test_get_returns_none_for_missing_scope(repo: TenantConfigRepository) -> None:
    assert repo.get("t1", "tenant", {}) is None


def test_get_returns_inserted_row(repo: TenantConfigRepository) -> None:
    repo.upsert(
        tenant_id="t1", layer="tier", scope={"customer_tier": "strategic"},
        weights={"po_number": 0.45}, created_by="alice",
    )
    got = repo.get("t1", "tier", {"customer_tier": "strategic"})
    assert got is not None
    assert got["weights"] == {"po_number": 0.45}


def test_list_by_layer_filters_correctly(repo: TenantConfigRepository) -> None:
    repo.upsert("t1", "tier", {"customer_tier": "strategic"},
                {"po_number": 0.45}, "alice")
    repo.upsert("t1", "tier", {"customer_tier": "standard"},
                {"po_number": 0.40}, "alice")
    repo.upsert("t1", "customer", {"customer_id": "CUST-1"},
                {"po_number": 0.35}, "alice")
    tier_rows = repo.list_by_layer("t1", "tier")
    assert len(tier_rows) == 2
    customer_rows = repo.list_by_layer("t1", "customer")
    assert len(customer_rows) == 1


def test_delete_returns_false_for_missing(repo: TenantConfigRepository) -> None:
    assert repo.delete("t1", "tenant", {}) is False


def test_delete_returns_true_and_removes_row(repo: TenantConfigRepository) -> None:
    repo.upsert("t1", "tenant", {}, {"po_number": 0.4}, "alice")
    assert repo.delete("t1", "tenant", {}) is True
    assert repo.get("t1", "tenant", {}) is None


def test_invalid_layer_raises(repo: TenantConfigRepository) -> None:
    with pytest.raises(ValueError):
        repo.upsert("t1", "rogue", {}, {"po_number": 0.4}, "alice")


def test_resolve_layered_overrides_empty(repo: TenantConfigRepository) -> None:
    result = repo.resolve_layered_overrides("t1")
    assert result == {"tenant": {}, "tier": {}, "customer": {}, "channel": {}}


def test_resolve_layered_overrides_full_stack(repo: TenantConfigRepository) -> None:
    repo.upsert("t1", "tenant", {}, {"po_number": 0.4}, "a")
    repo.upsert("t1", "tier", {"customer_tier": "strategic"},
                {"po_number": 0.45}, "a")
    repo.upsert("t1", "customer", {"customer_id": "CUST-1"},
                {"po_number": 0.5}, "a")
    repo.upsert("t1", "channel",
                {"customer_id": "CUST-1", "channel": "EDI"},
                {"po_number": 0.55}, "a")
    result = repo.resolve_layered_overrides(
        "t1",
        customer_tier="strategic",
        customer_id="CUST-1",
        channel="EDI",
    )
    assert result["tenant"] == {"po_number": 0.4}
    assert result["tier"] == {"po_number": 0.45}
    assert result["customer"] == {"po_number": 0.5}
    assert result["channel"] == {"po_number": 0.55}


def test_resolve_skips_layers_when_scope_missing(repo: TenantConfigRepository) -> None:
    repo.upsert("t1", "tier", {"customer_tier": "strategic"},
                {"po_number": 0.45}, "a")
    # No customer_tier provided → tier returns empty
    result = repo.resolve_layered_overrides("t1")
    assert result["tier"] == {}


def test_tenant_isolation(repo: TenantConfigRepository) -> None:
    repo.upsert("t1", "tenant", {}, {"po_number": 0.4}, "a")
    repo.upsert("t2", "tenant", {}, {"po_number": 0.5}, "a")
    t1_row = repo.get("t1", "tenant", {})
    t2_row = repo.get("t2", "tenant", {})
    assert t1_row is not None and t1_row["weights"] == {"po_number": 0.4}
    assert t2_row is not None and t2_row["weights"] == {"po_number": 0.5}
    # list_by_layer must not leak across tenants
    t1_rows = repo.list_by_layer("t1", "tenant")
    t2_rows = repo.list_by_layer("t2", "tenant")
    assert len(t1_rows) == 1
    assert len(t2_rows) == 1
    assert t1_rows[0]["tenant_id"] == "t1"
    assert t2_rows[0]["tenant_id"] == "t2"
