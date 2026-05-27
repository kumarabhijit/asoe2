"""Per-case child-intents cache tests (Phase 28.5.x §D2)."""
from __future__ import annotations

import pytest

from api import case_intents_cache
from api.case_events import publish_case_open, publish_case_update
from api.pubsub import event_publisher
from api.store import case_store, exception_store
from contracts.models import OrderCase


@pytest.fixture(autouse=True)
def _reset():
    case_intents_cache.clear()
    case_store.clear()
    if hasattr(exception_store, "clear"):
        exception_store.clear()
    if hasattr(event_publisher, "clear"):
        event_publisher.clear()
    yield
    case_intents_cache.clear()
    case_store.clear()


def _seed_case_with_children(tenant_id: str, intents: list[str]) -> str:
    case, _ = case_store.lookup_or_create(
        tenant_id=tenant_id,
        origin="CUSTOMER",
        source_channel="email",
        customer_po_number=f"PO-CACHE-{len(intents)}",
    )
    for i, intent in enumerate(intents):
        exception_store.create(
            tenant_id=tenant_id,
            order_id=f"PO-{i}",
            event_type="EDI_850_PRICE_MISMATCH",
            trace_id=f"trace-{i}",
            intent=intent,
            shadow_verdict="GREEN",
            parent_case_id=case.case_id,
        )
    return case.case_id


class TestIntentsFor:
    def test_returns_deduped_set_from_children(self):
        cid = _seed_case_with_children("tenant-a", [
            "CONTRACTUAL_CORRECTION",
            "DUPLICATE_PO",
            "CONTRACTUAL_CORRECTION",  # dup
        ])
        intents = case_intents_cache.intents_for("tenant-a", cid)
        assert intents == frozenset({"CONTRACTUAL_CORRECTION", "DUPLICATE_PO"})

    def test_empty_set_for_case_with_no_children(self):
        case, _ = case_store.lookup_or_create(
            tenant_id="tenant-a",
            origin="CUSTOMER",
            source_channel="email",
            customer_po_number="PO-EMPTY",
        )
        assert case_intents_cache.intents_for("tenant-a", case.case_id) == frozenset()

    def test_subsequent_read_is_cached(self, monkeypatch):
        cid = _seed_case_with_children("tenant-a", ["DUPLICATE_PO"])
        # Prime the cache.
        first = case_intents_cache.intents_for("tenant-a", cid)
        # Monkey-patch list_by_case to fail — proves the cache is
        # consulted without re-reading the store.
        monkeypatch.setattr(
            exception_store,
            "list_by_case",
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("should not be called on cache hit"),
            ),
        )
        second = case_intents_cache.intents_for("tenant-a", cid)
        assert first == second
        assert second == frozenset({"DUPLICATE_PO"})


class TestMatchesAny:
    def test_none_or_empty_filter_matches_everything(self):
        cid = _seed_case_with_children("tenant-a", ["DUPLICATE_PO"])
        assert case_intents_cache.matches_any("tenant-a", cid, None) is True
        assert case_intents_cache.matches_any(
            "tenant-a", cid, frozenset(),
        ) is True

    def test_overlap_matches(self):
        cid = _seed_case_with_children("tenant-a", [
            "DUPLICATE_PO", "BACK_ORDER_OOS",
        ])
        assert case_intents_cache.matches_any(
            "tenant-a", cid, frozenset({"DUPLICATE_PO"}),
        ) is True
        assert case_intents_cache.matches_any(
            "tenant-a", cid, frozenset({"BACK_ORDER_OOS", "DELIVERY_DELAY"}),
        ) is True

    def test_no_overlap_does_not_match(self):
        cid = _seed_case_with_children("tenant-a", ["DUPLICATE_PO"])
        assert case_intents_cache.matches_any(
            "tenant-a", cid, frozenset({"BACK_ORDER_OOS"}),
        ) is False


class TestInvalidation:
    def test_publish_case_open_invalidates(self):
        # Seed two children, prime cache.
        cid = _seed_case_with_children("tenant-a", ["DUPLICATE_PO"])
        case = case_store.get(cid)
        assert case_intents_cache.intents_for("tenant-a", cid) == frozenset(
            {"DUPLICATE_PO"},
        )
        # Attach a new child AFTER cache prime.
        exception_store.create(
            tenant_id="tenant-a",
            order_id="PO-NEW",
            event_type="EDI_850_PRICE_MISMATCH",
            trace_id="trace-new",
            intent="CONTRACTUAL_CORRECTION",
            shadow_verdict="GREEN",
            parent_case_id=cid,
        )
        # Without invalidation, cache would still report just DUPLICATE_PO.
        # The publish_case_open path is the canonical invalidator.
        publish_case_open(case)
        intents = case_intents_cache.intents_for("tenant-a", cid)
        assert intents == frozenset({"DUPLICATE_PO", "CONTRACTUAL_CORRECTION"})

    def test_publish_case_update_invalidates(self):
        cid = _seed_case_with_children("tenant-a", ["DUPLICATE_PO"])
        case = case_store.get(cid)
        _ = case_intents_cache.intents_for("tenant-a", cid)
        exception_store.create(
            tenant_id="tenant-a",
            order_id="PO-NEW",
            event_type="EDI_850_PRICE_MISMATCH",
            trace_id="trace-new",
            intent="BACK_ORDER_OOS",
            shadow_verdict="GREEN",
            parent_case_id=cid,
        )
        publish_case_update(case, updated_fields=["status"])
        assert case_intents_cache.intents_for("tenant-a", cid) == frozenset(
            {"DUPLICATE_PO", "BACK_ORDER_OOS"},
        )
