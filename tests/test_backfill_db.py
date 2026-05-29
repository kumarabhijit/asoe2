"""Phase H.7 — orphan-case backfill against the DB-backed stores.

The old backfill poked the in-memory ``_records`` / ``_cases`` dicts and
so was a no-op on the DB path (and never persisted ``parent_case_id``).
These tests inject DB-backed stores (SQLite always, Postgres when
``ASOE_TEST_POSTGRES_URL`` is set) and assert the backfill (a) opens
durable cases and (b) persists the child→parent link such that a fresh
store instance — i.e. a container restart — still sees it.
"""

from __future__ import annotations

import os
import uuid

import pytest

from agents.backfill import backfill_orphan_cases, merge_orphan_cases_by_correlation
from api.store import DatabaseBackedCaseStore, DatabaseBackedStore
from contracts.models import OrderEvent


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


_DB_BACKENDS = ["sqlite"] + (["postgres"] if _pg_available() else [])


def _url(backend: str, tmp_path) -> str:
    return f"sqlite:///{tmp_path}/backfill.db" if backend == "sqlite" else _pg_url()


def _orphan_event(order_id: str) -> dict:
    return OrderEvent(
        order_id=order_id, po_price=100.0, sap_base_price=110.0,
        event_type="EDI_850_PRICE_MISMATCH", retailer_id="walmart",
    ).model_dump()


@pytest.mark.parametrize("backend", _DB_BACKENDS)
def test_pass1_opens_durable_cases_and_persists_link(backend, tmp_path):
    url = _url(backend, tmp_path)
    tenant = "t-" + uuid.uuid4().hex[:12]
    exc = DatabaseBackedStore(url)
    cse = DatabaseBackedCaseStore(url)

    rec = exc.create(
        tenant, "PO-ORPH-1", "EDI_850_PRICE_MISMATCH", str(uuid.uuid4()),
        intent="DUPLICATE_PO", original_event=_orphan_event("PO-ORPH-1"),
    )
    assert rec.parent_case_id is None

    report = backfill_orphan_cases(tenant, exception_store=exc, case_store=cse)
    assert report.records_scanned == 1
    assert report.cases_opened == 1

    # Link is persisted (not just set on a transient object) AND survives
    # a fresh store instance — the durability guarantee.
    exc2 = DatabaseBackedStore(url)
    cse2 = DatabaseBackedCaseStore(url)
    reloaded = exc2.get(rec.id, tenant)
    assert reloaded.parent_case_id == report.record_to_case[rec.id]
    assert cse2.get(reloaded.parent_case_id) is not None


@pytest.mark.parametrize("backend", _DB_BACKENDS)
def test_pass1_idempotent_on_db(backend, tmp_path):
    url = _url(backend, tmp_path)
    tenant = "t-" + uuid.uuid4().hex[:12]
    exc = DatabaseBackedStore(url)
    cse = DatabaseBackedCaseStore(url)
    exc.create(tenant, "PO-IDEM", "EDI_850_PRICE_MISMATCH", str(uuid.uuid4()),
               original_event=_orphan_event("PO-IDEM"))

    first = backfill_orphan_cases(tenant, exception_store=exc, case_store=cse)
    second = backfill_orphan_cases(tenant, exception_store=exc, case_store=cse)
    assert first.cases_opened == 1
    # No orphans remain — the persisted link makes the rerun a true no-op.
    assert second.records_scanned == 0
    assert second.cases_opened == 0


@pytest.mark.parametrize("backend", _DB_BACKENDS)
def test_pass1_same_po_attaches_to_one_case(backend, tmp_path):
    url = _url(backend, tmp_path)
    tenant = "t-" + uuid.uuid4().hex[:12]
    exc = DatabaseBackedStore(url)
    cse = DatabaseBackedCaseStore(url)
    a = exc.create(tenant, "PO-SAME", "EDI_850_PRICE_MISMATCH", str(uuid.uuid4()),
                   original_event=_orphan_event("PO-SAME"))
    b = exc.create(tenant, "PO-SAME", "EDI_850_PRICE_MISMATCH", str(uuid.uuid4()),
                   original_event=_orphan_event("PO-SAME"))

    report = backfill_orphan_cases(tenant, exception_store=exc, case_store=cse)
    assert report.cases_opened == 1
    assert report.records_attached_to_existing == 1
    assert exc.get(a.id, tenant).parent_case_id == exc.get(b.id, tenant).parent_case_id


@pytest.mark.parametrize("backend", _DB_BACKENDS)
def test_pass2_merges_duplicate_cases(backend, tmp_path):
    url = _url(backend, tmp_path)
    tenant = "t-" + uuid.uuid4().hex[:12]
    exc = DatabaseBackedStore(url)
    cse = DatabaseBackedCaseStore(url)

    # Two distinct cases that come to share a PO (the interleaved-arrival
    # edge Pass 1 can't collapse): open via different keys, then stamp the
    # same customer_po onto both.
    case_a, _ = cse.lookup_or_create(tenant, origin="API",
                                     source_channel="edi_x12_850", sales_order_id="SO-A")
    case_b, _ = cse.lookup_or_create(tenant, origin="API",
                                     source_channel="edi_x12_850", sales_order_id="SO-B")
    cse.update(case_a.case_id, customer_po_number="PO-MERGE")
    cse.update(case_b.case_id, customer_po_number="PO-MERGE")
    # A child on each case.
    child_b = exc.create(tenant, "X", "T", str(uuid.uuid4()),
                         parent_case_id=case_b.case_id)

    # Dry-run reports the candidate without mutating.
    dry = merge_orphan_cases_by_correlation(tenant, dry_run=True,
                                            exception_store=exc, case_store=cse)
    assert dry.cases_merged == 1
    assert cse.get(case_b.case_id) is not None

    # Real merge: earliest case wins, duplicate dropped, child re-pointed.
    report = merge_orphan_cases_by_correlation(tenant, exception_store=exc, case_store=cse)
    assert report.cases_merged == 1
    primary, duplicate = sorted([case_a, case_b], key=lambda c: c.opened_at)
    assert cse.get(primary.case_id) is not None
    assert cse.get(duplicate.case_id) is None
    assert exc.get(child_b.id, tenant).parent_case_id == primary.case_id
