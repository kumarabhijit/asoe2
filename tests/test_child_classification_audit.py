"""Phase 5 — child-case leaf reclassification writes an audit row.

Requirements §8.6 says "every classification or reclassification event
appends exactly one row." Phase 3 covered case-level reclassification
(via ``CaseStore.update``); this commit closes the child-leaf branch
by wiring ``ExceptionStore.update`` and ``DatabaseBackedStore.update``
to call ``CaseStore._record_for_child`` whenever ``intent_code`` or
``supergroup_code`` changes on a child case.

Acceptance criterion #9 is now fully met.
"""

from __future__ import annotations

import pytest

from api.store import case_store, exception_store


@pytest.fixture(autouse=True)
def _reset_stores():
    case_store.clear() if hasattr(case_store, "clear") else None
    exception_store.clear()
    yield
    case_store.clear() if hasattr(case_store, "clear") else None
    exception_store.clear()


def _open_case_with_child():
    case, _ = case_store.lookup_or_create(
        tenant_id="t1", origin="API", source_channel="edi_x12_850",
        sales_order_id="SO-CHILD-AUDIT",
        supergroup_code="SG_BLOCK_PRICING",
        intent_code="INT_PRICE_MISMATCH",
    )
    child = exception_store.create(
        tenant_id="t1", order_id="SO-CHILD-AUDIT",
        event_type="EDI_850_PRICE_MISMATCH",
        trace_id="trace-1",
        parent_case_id=case.case_id,
        supergroup_code="SG_BLOCK_PRICING",
        intent_code="INT_PRICE_MISMATCH",
    )
    return case, child


def test_child_intent_change_writes_audit_row():
    """Leaf-intent reclassification on a child writes one event to the
    parent case's history, with ``child_case_id`` set."""
    case, child = _open_case_with_child()
    history_before = len(case_store.get_classification_history(case.case_id))

    exception_store.update(
        child.id, tenant_id="t1",
        intent_code="INT_MASS_PRICING_ERROR",
        classified_by="user:lead-1", classifier_type="HUMAN",
        reason_text="Re-classified after pricing review",
    )

    history = case_store.get_classification_history(case.case_id)
    assert len(history) == history_before + 1
    new_event = history[-1]
    assert new_event.child_case_id == child.id
    assert new_event.intent_code == "INT_MASS_PRICING_ERROR"
    assert new_event.classified_by == "user:lead-1"
    assert new_event.classifier_type == "HUMAN"
    assert new_event.reason_text == "Re-classified after pricing review"


def test_child_supergroup_and_intent_atomic_change_writes_one_row():
    """An atomic update that changes both intent and supergroup writes
    exactly ONE history row, not two."""
    case, child = _open_case_with_child()
    history_before = len(case_store.get_classification_history(case.case_id))

    exception_store.update(
        child.id, tenant_id="t1",
        supergroup_code="SG_BLOCK_CREDIT",
        intent_code="INT_CREDIT_BLOCK",
        classified_by="user:csr-2", classifier_type="HUMAN",
    )
    history = case_store.get_classification_history(case.case_id)
    assert len(history) == history_before + 1
    new_event = history[-1]
    assert new_event.supergroup_code == "SG_BLOCK_CREDIT"
    assert new_event.intent_code == "INT_CREDIT_BLOCK"


def test_child_update_without_intent_or_supergroup_writes_no_row():
    """A non-classification field update on the child (e.g. setting
    lifecycle_state) does not append a history row."""
    case, child = _open_case_with_child()
    history_before = len(case_store.get_classification_history(case.case_id))

    exception_store.update(
        child.id, tenant_id="t1",
        lifecycle_state="RESOLVED",
    )

    assert (
        len(case_store.get_classification_history(case.case_id))
        == history_before
    )


def test_child_intent_unchanged_no_row():
    """Setting intent_code to the same value is a no-op for the audit
    (consistent with the case-level treatment)."""
    case, child = _open_case_with_child()
    history_before = len(case_store.get_classification_history(case.case_id))

    exception_store.update(
        child.id, tenant_id="t1",
        intent_code="INT_PRICE_MISMATCH",  # unchanged
        classified_by="user:csr-1", classifier_type="HUMAN",
    )

    assert (
        len(case_store.get_classification_history(case.case_id))
        == history_before
    )


def test_child_reclassification_requires_classifier_kwargs():
    """Audit invariant — leaf changes must be attributable."""
    case, child = _open_case_with_child()
    with pytest.raises(ValueError, match="classified_by"):
        exception_store.update(
            child.id, tenant_id="t1",
            intent_code="INT_MASS_PRICING_ERROR",
        )


def test_orphan_child_reclassification_does_not_crash():
    """A child without a parent_case_id (legacy / Tier-1 stateless)
    can be reclassified — the audit step is skipped silently because
    there is no case to attach the event to."""
    exception_store.clear()
    orphan = exception_store.create(
        tenant_id="t1", order_id="SO-ORPHAN-1",
        event_type="EDI_850_PRICE_MISMATCH",
        trace_id="trace-orphan",
        supergroup_code="SG_BLOCK_PRICING",
        intent_code="INT_PRICE_MISMATCH",
    )
    assert orphan.parent_case_id is None
    exception_store.update(
        orphan.id, tenant_id="t1",
        intent_code="INT_MASS_PRICING_ERROR",
        classified_by="user:csr-1", classifier_type="HUMAN",
    )
