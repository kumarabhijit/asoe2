"""ADR-038 Phase H.2 — OrderCase + correlation table tests.

Locks the case primitive's contracts:
  * Pydantic OrderCase model accepts valid inputs and rejects
    out-of-vocabulary source / status / tier values.
  * CaseStore.lookup_or_create resolves correlation keys per the
    SO → PO → EDI txn → email priority (ADR-038 §6.2).
  * Multi-PO email opens N cases.
  * Email matching pre-existing Automated case attaches (no new
    case opened); first event's source/source_channel preserved.
  * Tenant isolation — two tenants with the same correlation key
    do NOT share a case.
  * ExceptionRecord.parent_case_id is wired and defaults to None
    for Tier-1 stateless records.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.store import CaseStore, ExceptionRecord, case_store
from contracts.models import OrderCase


@pytest.fixture(autouse=True)
def _reset_case_store():
    """Each test starts with a fresh in-memory store."""
    case_store.clear()
    yield
    case_store.clear()


# ---------------------------------------------------------------------------
# Pydantic model validation
# ---------------------------------------------------------------------------


class TestOrderCaseModel:
    def test_minimal_valid_case(self):
        case = OrderCase(
            tenant_id="t1",
            origin="CUSTOMER",
            source_channel="email",
        )
        assert case.case_id  # auto-generated UUID
        assert case.opened_at  # auto-generated ISO timestamp
        # Issue #133 PO #17 — updated_at defaults to "now" on creation
        # and is bumped on every CaseStore mutation. On a brand new
        # case it equals opened_at (both stamped during model init).
        assert case.updated_at
        assert case.updated_at >= case.opened_at
        assert case.status == "OPEN_AGENT_PROCESSING"  # default
        assert case.tier == 2  # default

    def test_updated_at_bumped_on_store_update(self):
        # PO #17 — every mutation routed through CaseStore.update
        # must bump updated_at, even when the caller doesn't pass it.
        store = CaseStore()
        case = OrderCase(
            tenant_id="t1",
            origin="CUSTOMER",
            source_channel="email",
        )
        store._cases[case.case_id] = case  # pre-seed
        original = case.updated_at
        # Sleep a hair to guarantee a different ISO timestamp; the
        # mutation should land on a strictly-later updated_at.
        import time
        time.sleep(0.001)
        bumped = store.update(case.case_id, status="OPEN_AWAITING_HUMAN")
        assert bumped.updated_at > original
        assert bumped.status == "OPEN_AWAITING_HUMAN"

    def test_updated_at_explicit_override_honoured(self):
        # Audit-replay path can stamp a specific timestamp; the auto-
        # bump must not clobber an explicit value the caller passed.
        store = CaseStore()
        case = OrderCase(
            tenant_id="t1",
            origin="CUSTOMER",
            source_channel="email",
        )
        store._cases[case.case_id] = case
        explicit = "2020-01-01T00:00:00+00:00"
        bumped = store.update(case.case_id, updated_at=explicit)
        assert bumped.updated_at == explicit

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            OrderCase(
                tenant_id="t1",
                origin="CUSTOMER",
                source_channel="email",
                unknown_field="x",  # type: ignore[call-arg]
            )

    def test_invalid_source_rejected(self):
        with pytest.raises(ValidationError):
            OrderCase(
                tenant_id="t1",
                source="not_a_source",  # type: ignore[arg-type]
                source_channel="email",
            )

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            OrderCase(
                tenant_id="t1",
                origin="CUSTOMER",
                source_channel="email",
                status="WHATEVER",  # type: ignore[arg-type]
            )

    def test_invalid_tier_rejected(self):
        with pytest.raises(ValidationError):
            OrderCase(
                tenant_id="t1",
                origin="CUSTOMER",
                source_channel="email",
                tier=5,  # type: ignore[arg-type]
            )

    def test_correlation_keys_optional(self):
        # All four correlation keys can be None (case opened before any
        # external identifiers materialise — uncommon but valid).
        case = OrderCase(
            tenant_id="t1",
            origin="CUSTOMER",
            source_channel="email",
        )
        assert case.customer_po_number is None
        assert case.sales_order_id is None
        assert case.edi_transaction_id is None
        assert case.source_email_id is None


# ---------------------------------------------------------------------------
# CaseStore — lookup_or_create
# ---------------------------------------------------------------------------


class TestCaseStoreLookupOrCreate:
    def test_first_event_opens_a_case(self):
        case, opened_now = case_store.lookup_or_create(
            tenant_id="t1",
            origin="CUSTOMER",
            source_channel="email",
            customer_po_number="PO-1234",
            source_email_id="msg-1",
        )
        assert opened_now is True
        assert case.case_id
        assert case.customer_po_number == "PO-1234"

    def test_second_event_with_same_po_attaches_to_existing_case(self):
        case_a, opened_a = case_store.lookup_or_create(
            tenant_id="t1",
            origin="CUSTOMER",
            source_channel="email",
            customer_po_number="PO-1234",
            source_email_id="msg-1",
        )
        # Second event from the same buyer — different email, same PO.
        case_b, opened_b = case_store.lookup_or_create(
            tenant_id="t1",
            origin="CUSTOMER",
            source_channel="email",
            customer_po_number="PO-1234",
            source_email_id="msg-2",  # different email id
        )
        assert opened_a is True
        assert opened_b is False
        assert case_a.case_id == case_b.case_id

    def test_second_event_with_same_so_id_attaches(self):
        case_a, _ = case_store.lookup_or_create(
            tenant_id="t1",
            origin="API",
            source_channel="edi_x12_850",
            customer_po_number="PO-1234",
            sales_order_id="SO-9988",
        )
        case_b, opened_b = case_store.lookup_or_create(
            tenant_id="t1",
            origin="API",
            source_channel="edi_x12_855",
            sales_order_id="SO-9988",  # SO match
        )
        assert opened_b is False
        assert case_a.case_id == case_b.case_id

    def test_so_id_takes_priority_over_po_number(self):
        """ADR-038 §6.2 — SO id wins over PO number when both present."""
        # Case A: opened on PO-A
        case_a, _ = case_store.lookup_or_create(
            tenant_id="t1",
            origin="CUSTOMER",
            source_channel="email",
            customer_po_number="PO-A",
        )
        # Case B: opened on SO-A
        case_b, _ = case_store.lookup_or_create(
            tenant_id="t1",
            origin="API",
            source_channel="edi_x12_850",
            sales_order_id="SO-A",
        )
        assert case_a.case_id != case_b.case_id

        # New event carries BOTH PO-A (matches case_a) AND SO-A (matches case_b).
        # Per ADR-038 §6.2 priority, SO id wins → resolves to case_b.
        resolved, opened_new = case_store.lookup_or_create(
            tenant_id="t1",
            origin="API",
            source_channel="edi_x12_855",
            customer_po_number="PO-A",
            sales_order_id="SO-A",
        )
        assert opened_new is False
        assert resolved.case_id == case_b.case_id

    def test_email_to_existing_automated_case_attaches(self):
        """ADR-038 §6.2 — Manual email matching pre-existing Automated
        case attaches; new case is NOT opened."""
        # Phase 1: EDI 850 opens an Automated Order case on PO-1234.
        edi_case, _ = case_store.lookup_or_create(
            tenant_id="t1",
            origin="API",
            source_channel="edi_x12_850",
            customer_po_number="PO-1234",
        )
        # Phase 2: customer email arrives referencing the same PO.
        email_case, opened = case_store.lookup_or_create(
            tenant_id="t1",
            origin="CUSTOMER",  # would-be source if new case opened
            source_channel="email",
            customer_po_number="PO-1234",
            source_email_id="msg-1",
        )
        # Same case as the EDI run; source stays automated_order
        # (immutable per ADR-038 §3.1; source_channel could become a
        # list in a future commit but the V1 model keeps it scalar).
        assert opened is False
        assert email_case.case_id == edi_case.case_id
        assert email_case.origin == "API"

    def test_tenant_isolation(self):
        """Two tenants with the same correlation key do NOT share a case."""
        case_a, _ = case_store.lookup_or_create(
            tenant_id="t1",
            origin="CUSTOMER",
            source_channel="email",
            customer_po_number="PO-1234",
        )
        case_b, opened_b = case_store.lookup_or_create(
            tenant_id="t2",  # different tenant
            origin="CUSTOMER",
            source_channel="email",
            customer_po_number="PO-1234",
        )
        assert opened_b is True
        assert case_a.case_id != case_b.case_id

    def test_multi_po_email_opens_n_cases(self):
        """ADR-038 §6.2 — caller splits a multi-PO email into N
        invocations, one per PO. Each opens its own case."""
        case_a, opened_a = case_store.lookup_or_create(
            tenant_id="t1",
            origin="CUSTOMER",
            source_channel="email",
            customer_po_number="PO-A",
            source_email_id="msg-1",
        )
        # Same email, second PO. We deliberately omit source_email_id
        # on the second invocation because using the same email_id
        # would resolve to case_a via that key (correctly: the email
        # is one substrate); per ADR-038 §6.2 the multi-PO case opens
        # N cases by suppressing the email correlation for all but one.
        case_b, opened_b = case_store.lookup_or_create(
            tenant_id="t1",
            origin="CUSTOMER",
            source_channel="email",
            customer_po_number="PO-B",
        )
        assert opened_a is True
        assert opened_b is True
        assert case_a.case_id != case_b.case_id

    def test_correlation_keys_enrich_existing_case(self):
        """When a later event carries a NEW correlation key, the key
        joins the existing case (e.g. ERP creates the SO and the SO
        id joins the case opened on the PO number)."""
        case_a, _ = case_store.lookup_or_create(
            tenant_id="t1",
            origin="API",
            source_channel="edi_x12_850",
            customer_po_number="PO-1234",
        )
        # Later: SO is created in ERP. Event attaches via PO match;
        # the SO id should now also resolve to the same case.
        case_b, opened = case_store.lookup_or_create(
            tenant_id="t1",
            origin="API",
            source_channel="edi_x12_850",
            customer_po_number="PO-1234",
            sales_order_id="SO-9988",
        )
        assert opened is False
        assert case_b.case_id == case_a.case_id

        # Subsequent event keyed only on SO must resolve to the same case.
        case_c, opened_c = case_store.lookup_or_create(
            tenant_id="t1",
            origin="API",
            source_channel="edi_x12_855",
            sales_order_id="SO-9988",
        )
        assert opened_c is False
        assert case_c.case_id == case_a.case_id


# ---------------------------------------------------------------------------
# CaseStore — CRUD
# ---------------------------------------------------------------------------


class TestCaseStoreCRUD:
    def test_get_returns_none_when_unknown(self):
        assert case_store.get("nope") is None

    def test_get_returns_existing_case(self):
        case, _ = case_store.lookup_or_create(
            tenant_id="t1",
            origin="CUSTOMER",
            source_channel="email",
            customer_po_number="PO-1234",
        )
        retrieved = case_store.get(case.case_id)
        assert retrieved is not None
        assert retrieved.case_id == case.case_id

    def test_update_modifies_fields(self):
        case, _ = case_store.lookup_or_create(
            tenant_id="t1",
            origin="CUSTOMER",
            source_channel="email",
            customer_po_number="PO-1234",
        )
        updated = case_store.update(case.case_id, status="OPEN_AWAITING_HUMAN")
        assert updated.status == "OPEN_AWAITING_HUMAN"
        # Confirm persistence: re-read.
        re_read = case_store.get(case.case_id)
        assert re_read.status == "OPEN_AWAITING_HUMAN"

    def test_update_unknown_raises(self):
        with pytest.raises(KeyError):
            case_store.update("nope", status="RESOLVED")

    def test_list_by_tenant_filters(self):
        case_store.lookup_or_create(
            tenant_id="t1",
            origin="CUSTOMER",
            source_channel="email",
            customer_po_number="PO-1",
        )
        case_store.lookup_or_create(
            tenant_id="t2",
            origin="CUSTOMER",
            source_channel="email",
            customer_po_number="PO-2",
        )
        t1_cases = case_store.list_by_tenant("t1")
        t2_cases = case_store.list_by_tenant("t2")
        assert len(t1_cases) == 1
        assert len(t2_cases) == 1
        assert t1_cases[0].tenant_id == "t1"

    def test_register_correlation_attaches_new_key(self):
        case, _ = case_store.lookup_or_create(
            tenant_id="t1",
            origin="CUSTOMER",
            source_channel="email",
            source_email_id="msg-1",
        )
        # Later: ERP creates SO; harness registers the new key.
        case_store.register_correlation(
            "t1", case.case_id, "sales_order_id", "SO-9988",
        )
        # Subsequent SO-keyed event resolves to the same case.
        resolved, opened = case_store.lookup_or_create(
            tenant_id="t1",
            origin="API",
            source_channel="edi_x12_855",
            sales_order_id="SO-9988",
        )
        assert opened is False
        assert resolved.case_id == case.case_id


# ---------------------------------------------------------------------------
# ExceptionRecord.parent_case_id wiring
# ---------------------------------------------------------------------------


class TestExceptionRecordParentCaseId:
    def test_default_is_none_for_tier_1(self):
        """Tier-1 stateless records (clean Automated) carry None
        parent_case_id per ADR-038 §7.2."""
        record = ExceptionRecord(
            tenant_id="t1",
            order_id="SO-1",
            event_type="EDI_850_PRICE_MISMATCH",
            trace_id="trace-1",
        )
        assert record.parent_case_id is None

    def test_parent_case_id_wired_from_constructor(self):
        case, _ = case_store.lookup_or_create(
            tenant_id="t1",
            origin="CUSTOMER",
            source_channel="email",
            customer_po_number="PO-1234",
        )
        record = ExceptionRecord(
            tenant_id="t1",
            order_id="PO-1234",
            event_type="EMAIL_ORDER_ENTRY_REQUEST",
            trace_id="trace-1",
            parent_case_id=case.case_id,
        )
        assert record.parent_case_id == case.case_id
