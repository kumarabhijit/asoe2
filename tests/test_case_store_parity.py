"""Phase H.7 — CaseStore parity + durability.

Runs the SAME behavioural assertions against the in-memory ``CaseStore``
and the DB-backed ``DatabaseBackedCaseStore`` (SQLite always, Postgres
when ``ASOE_TEST_POSTGRES_URL`` is set) so the two implementations cannot
drift. Plus the headline H.7 acceptance test: cases survive a fresh store
instance (i.e. a container restart) on the DB-backed path.

Supergroup codes used here (SG_BLOCK_PRICING / SG_BLOCK_CREDIT /
SG_NEEDS_TRIAGE) must exist in the V017-seeded taxonomy because the
DB-backed path enforces the order_case.supergroup_code FK; the in-memory
store has no such constraint, so using real codes keeps both green.
"""

from __future__ import annotations

import os
import uuid

import pytest

from api.store import (
    CaseStore,
    DatabaseBackedCaseStore,
    NeedsTriageCloseBlocked,
)
from contracts._generated.taxonomy_constants import SG_NEEDS_TRIAGE
from contracts.models import CasePendingOverride


def _pg_url() -> str | None:
    return os.getenv("ASOE_TEST_POSTGRES_URL")


def _pg_available() -> bool:
    url = _pg_url()
    if not url:
        return False
    try:
        import psycopg2
        psycopg2.connect(url).close()
        return True
    except Exception:
        return False


_BACKENDS = ["memory", "sqlite"]
if _pg_available():
    _BACKENDS.append("postgres")

_DB_BACKENDS = [b for b in _BACKENDS if b != "memory"]


def _make_store(backend: str, tmp_path):
    if backend == "memory":
        return CaseStore()
    if backend == "sqlite":
        return DatabaseBackedCaseStore(f"sqlite:///{tmp_path}/case.db")
    return DatabaseBackedCaseStore(_pg_url())


@pytest.fixture(params=_BACKENDS)
def store(request, tmp_path):
    # Postgres rows are isolated per test by a unique tenant (the V020
    # append-only trigger blocks a TRUNCATE-style reset), so no teardown
    # wipe is needed; SQLite gets a fresh file per test.
    return _make_store(request.param, tmp_path)


@pytest.fixture
def tenant() -> str:
    return "t-" + uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Parity — same assertions across memory + DB backends
# ---------------------------------------------------------------------------


def test_lookup_or_create_opens_then_attaches(store, tenant):
    case, opened = store.lookup_or_create(
        tenant, origin="API", source_channel="edi_x12_850",
        customer_po_number="PO-1",
    )
    assert opened is True
    # Same PO re-attaches to the same case (no second case).
    case2, opened2 = store.lookup_or_create(
        tenant, origin="API", source_channel="edi_x12_850",
        customer_po_number="PO-1",
    )
    assert opened2 is False
    assert case2.case_id == case.case_id
    assert len(store.list_by_tenant(tenant)) == 1


def test_find_by_correlation_priority(store, tenant):
    case, _ = store.lookup_or_create(
        tenant, origin="API", source_channel="edi_x12_850",
        customer_po_number="PO-2", sales_order_id="SO-2",
    )
    # SO and PO both resolve to the case.
    assert store.find_by_correlation(tenant, sales_order_id="SO-2").case_id == case.case_id
    assert store.find_by_correlation(tenant, customer_po_number="PO-2").case_id == case.case_id
    assert store.find_by_correlation(tenant, edi_transaction_id="nope") is None


def test_register_correlation_unknown_case_raises(store, tenant):
    with pytest.raises(KeyError):
        store.register_correlation(tenant, "no-such-case", "customer_po_number", "X")


def test_update_field_round_trip(store, tenant):
    case, _ = store.lookup_or_create(
        tenant, origin="API", source_channel="edi_x12_850", customer_po_number="PO-3",
    )
    updated = store.update(case.case_id, status="OPEN_AWAITING_HUMAN")
    assert updated.status == "OPEN_AWAITING_HUMAN"
    assert store.get(case.case_id).status == "OPEN_AWAITING_HUMAN"


def test_reclassification_requires_attribution_and_audits(store, tenant):
    case, _ = store.lookup_or_create(
        tenant, origin="API", source_channel="edi_x12_850",
        customer_po_number="PO-4", supergroup_code="SG_BLOCK_PRICING",
    )
    # Missing classified_by/classifier_type → ValueError.
    with pytest.raises(ValueError):
        store.update(case.case_id, supergroup_code="SG_BLOCK_CREDIT")
    # Attributed reclassification appends exactly one audit row.
    store.update(
        case.case_id, supergroup_code="SG_BLOCK_CREDIT",
        classified_by="user:csr", classifier_type="HUMAN",
    )
    hist = store.get_classification_history(case.case_id)
    # intake (SG_BLOCK_PRICING) + reclassify (SG_BLOCK_CREDIT)
    assert [h.supergroup_code for h in hist] == ["SG_BLOCK_PRICING", "SG_BLOCK_CREDIT"]
    assert hist[-1].classifier_type == "HUMAN"


def test_needs_triage_close_block(store, tenant):
    case, _ = store.lookup_or_create(
        tenant, origin="API", source_channel="edi_x12_850",
        customer_po_number="PO-5", supergroup_code=SG_NEEDS_TRIAGE,
    )
    with pytest.raises(NeedsTriageCloseBlocked):
        store.update(case.case_id, status="RESOLVED")


def test_pending_override_forward_only(store, tenant):
    case, _ = store.lookup_or_create(
        tenant, origin="API", source_channel="edi_x12_850", customer_po_number="PO-6",
    )
    ov = CasePendingOverride(
        initiator="u1", initiated_at="2026-01-01T00:00:00Z",
        pending_action="APPROVE_DESPITE_DOWNGRADE",
        child_exception_ids=["e1", "e2"], aggregate_financial_impact_usd=12.5,
    )
    after = store.set_pending_override(case.case_id, ov)
    assert after.pending_override is not None
    assert after.pending_override.child_exception_ids == ["e1", "e2"]
    assert after.status == "OPEN_AWAITING_HUMAN"
    # Second initiator is rejected (forward-only).
    with pytest.raises(ValueError):
        store.set_pending_override(case.case_id, ov)
    # Clear lifts it and allows a new one.
    store.clear_pending_override(case.case_id)
    assert store.get(case.case_id).pending_override is None
    store.set_pending_override(case.case_id, ov)


def test_list_by_tenant_isolation(store, tenant):
    store.lookup_or_create(tenant, origin="API", source_channel="edi_x12_850",
                           customer_po_number="PO-7")
    other = "t-" + uuid.uuid4().hex[:12]
    store.lookup_or_create(other, origin="API", source_channel="edi_x12_850",
                           customer_po_number="PO-7")
    assert len(store.list_by_tenant(tenant)) == 1
    assert len(store.list_by_tenant(other)) == 1


def test_get_unknown_returns_none(store):
    assert store.get("does-not-exist") is None


# ---------------------------------------------------------------------------
# Durability — the headline H.7 acceptance test (DB backends only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", _DB_BACKENDS)
def test_cases_survive_a_fresh_store_instance(backend, tmp_path):
    """A new DatabaseBackedCaseStore on the SAME database still sees the
    case, its correlation keys, and its classification history — i.e. the
    case survives a container restart (the defect this whole phase fixes).
    """
    tenant = "t-" + uuid.uuid4().hex[:12]
    url = (
        f"sqlite:///{tmp_path}/durable.db" if backend == "sqlite" else _pg_url()
    )
    store_a = DatabaseBackedCaseStore(url)
    case, opened = store_a.lookup_or_create(
        tenant, origin="API", source_channel="edi_x12_850",
        customer_po_number="PO-DURABLE", supergroup_code="SG_BLOCK_PRICING",
    )
    assert opened

    # Simulate a restart: brand-new store object, same backing store.
    store_b = DatabaseBackedCaseStore(url)
    reloaded = store_b.get(case.case_id)
    assert reloaded is not None
    assert reloaded.customer_po_number == "PO-DURABLE"
    assert store_b.find_by_correlation(tenant, customer_po_number="PO-DURABLE") is not None
    assert [c.case_id for c in store_b.list_by_tenant(tenant)] == [case.case_id]
    assert len(store_b.get_classification_history(case.case_id)) == 1
