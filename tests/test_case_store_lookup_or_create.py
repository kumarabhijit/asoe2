"""CaseStore.lookup_or_create — origin + supergroup_code behaviour.

Locks the kwarg contract after the Phase-2 sweep:
  * ``origin=`` is the only intake-mechanism axis (no `source=` shim).
  * ``supergroup_code=`` is accepted and round-trips onto the case.
  * Explicit values are preserved (no surprise defaulting).

Replaces the deleted TestLookupOrCreateCaseType block from the old
test_case_type_invariants.py (code-review finding #3).
"""

from __future__ import annotations

import pytest

from api.store import case_store


@pytest.fixture(autouse=True)
def _reset_case_store():
    case_store.clear() if hasattr(case_store, "clear") else None
    yield
    case_store.clear() if hasattr(case_store, "clear") else None


def test_customer_event_opens_with_origin_customer():
    case, opened = case_store.lookup_or_create(
        tenant_id="t1", origin="CUSTOMER", source_channel="email",
        customer_po_number="PO-CUST-1",
    )
    assert opened is True
    assert case.origin == "CUSTOMER"
    assert case.source_channel == "email"


def test_api_event_opens_with_origin_api():
    case, opened = case_store.lookup_or_create(
        tenant_id="t1", origin="API", source_channel="edi_x12_850",
        sales_order_id="SO-API-1",
    )
    assert opened is True
    assert case.origin == "API"
    assert case.source_channel == "edi_x12_850"


def test_supergroup_code_passes_through():
    """The Phase-3 classifier writes supergroup_code at open time; the
    kwarg lands on the OrderCase row, validated against the lookup table."""
    case, _ = case_store.lookup_or_create(
        tenant_id="t1", origin="API", source_channel="edi_x12_850",
        sales_order_id="SO-API-2",
        supergroup_code="SG_BLOCK_PRICING",
    )
    assert case.supergroup_code == "SG_BLOCK_PRICING"


def test_supergroup_code_unset_stays_none():
    case, _ = case_store.lookup_or_create(
        tenant_id="t1", origin="API", source_channel="edi_x12_850",
        sales_order_id="SO-API-3",
    )
    assert case.supergroup_code is None


def test_correlation_match_returns_existing_without_overwriting_origin():
    """Second event with a matching correlation key returns the existing
    case; the original origin is immutable across the dedup."""
    first, opened_a = case_store.lookup_or_create(
        tenant_id="t1", origin="CUSTOMER", source_channel="email",
        customer_po_number="PO-CORR-1",
    )
    assert opened_a is True
    # Same PO arrives a second time, this time tagged as API. Dedup wins;
    # origin is not silently overwritten.
    second, opened_b = case_store.lookup_or_create(
        tenant_id="t1", origin="API", source_channel="edi_x12_850",
        customer_po_number="PO-CORR-1",
    )
    assert opened_b is False
    assert second.case_id == first.case_id
    assert second.origin == "CUSTOMER"


def test_invalid_origin_rejected_at_construction():
    """``Origin`` is a Literal["CUSTOMER","API"]; anything else trips
    Pydantic ValidationError before the store records the row."""
    with pytest.raises(Exception):  # ValidationError
        case_store.lookup_or_create(
            tenant_id="t1", origin="BOTH", source_channel="email",
            customer_po_number="PO-BAD-1",
        )
