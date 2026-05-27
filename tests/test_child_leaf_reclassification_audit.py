"""Pin the known audit-gap for child-case leaf-intent reclassification.

Requirements §8.6 says "every classification or reclassification event
appends exactly one row." Today, CaseStore.update fires a history
event when a case's ``supergroup_code`` changes — but ChildCase leaf
intent changes go through ``ExceptionStore.update`` (or
``DatabaseBackedStore.update``) which has no audit hook. A leaf-only
correction (``intent_code: INT_A → INT_B`` on a child, same parent
super-group) therefore leaves no row.

This test pins the current behaviour so:
  1. A future commit that closes the gap (extends ExceptionStore.update
     to call ``case_store.record_classification`` with ``child_case_id``
     set) deliberately updates this test.
  2. A future commit that *introduces* a regression by writing a row
     somewhere unexpected is also caught.

Tracking: criterion #9 is partially met (case-level events covered);
the child-leaf branch is a follow-up commit.
"""

from __future__ import annotations

from api.store import case_store, exception_store


def test_child_intent_only_change_does_not_write_history():
    """Leaf-intent reclassification on a child case leaves the parent
    case's classification history untouched (today's gap)."""
    case_store.clear()
    exception_store.clear()

    case, _ = case_store.lookup_or_create(
        tenant_id="t1", origin="API", source_channel="edi_x12_850",
        sales_order_id="SO-GAP-1",
        supergroup_code="SG_BLOCK_PRICING",
        intent_code="INT_PRICE_MISMATCH",
    )
    child = exception_store.create(
        tenant_id="t1", order_id="SO-GAP-1", event_type="EDI_850_PRICE_MISMATCH",
        trace_id="trace-1",
        parent_case_id=case.case_id,
        supergroup_code="SG_BLOCK_PRICING",
        intent_code="INT_PRICE_MISMATCH",
    )

    history_before = len(case_store.get_classification_history(case.case_id))

    # Child leaf reclassification (same super-group, new intent_code).
    exception_store.update(
        child.id, tenant_id="t1",
        intent_code="INT_MASS_PRICING_ERROR",
    )

    # Today: no audit row appended for child-leaf changes. (Gap pinned.)
    history_after = len(case_store.get_classification_history(case.case_id))
    assert history_after == history_before


def test_child_supergroup_change_via_exception_store_also_un_audited():
    """Symmetric pin: even a super-group change on the child (which is
    an inheritance-trigger violation in the DB layer, but free in the
    in-memory store) is currently unaudited because ExceptionStore.update
    has no hook into CaseStore.record_classification."""
    case_store.clear()
    exception_store.clear()

    case, _ = case_store.lookup_or_create(
        tenant_id="t1", origin="API", source_channel="edi_x12_850",
        sales_order_id="SO-GAP-2",
        supergroup_code="SG_BLOCK_PRICING",
        intent_code="INT_PRICE_MISMATCH",
    )
    child = exception_store.create(
        tenant_id="t1", order_id="SO-GAP-2", event_type="EDI_850_PRICE_MISMATCH",
        trace_id="trace-2",
        parent_case_id=case.case_id,
        supergroup_code="SG_BLOCK_PRICING",
        intent_code="INT_PRICE_MISMATCH",
    )

    history_before = len(case_store.get_classification_history(case.case_id))
    exception_store.update(
        child.id, tenant_id="t1",
        supergroup_code="SG_BLOCK_CREDIT",
        intent_code="INT_CREDIT_BLOCK",
    )
    history_after = len(case_store.get_classification_history(case.case_id))
    assert history_after == history_before
