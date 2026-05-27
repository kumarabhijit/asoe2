"""ADR-038 Phase H.3 — case materialisation e2e tests.

End-to-end coverage for the case-resolver wiring in
``api/routes/exceptions.py::_persist_exception``. Verifies:

  * Clean API event (final_status == COMPLETE) → no case opened;
    record's ``parent_case_id`` is None (T1 stateless path).
  * Non-clean API event (MANUAL_REVIEW_REQUIRED / BLOCKED) →
    case opens; record's ``parent_case_id`` is set; case carries
    ``origin = "API"``.
  * CUSTOMER-origin event → case opens eagerly regardless of terminal
    status; ``origin = "CUSTOMER"``.
  * Multiple non-clean events for the same customer PO → all attach
    to the same case (correlation lookup-or-create works through the
    full e2e path).
"""

from __future__ import annotations

import pytest

from api.case_resolver import (
    derive_origin_and_channel,
    materialise_for_event,
    should_materialise,
)
from api.store import case_store, exception_store
from contracts.models import OrderEvent


@pytest.fixture(autouse=True)
def _reset_stores():
    case_store.clear()
    exception_store.clear()
    yield
    case_store.clear()
    exception_store.clear()


# ---------------------------------------------------------------------------
# derive_origin_and_channel — pure routing helper
# ---------------------------------------------------------------------------


class TestDeriveOriginAndChannel:
    def test_email_event_type_routes_to_customer(self):
        event = OrderEvent(
            order_id="EML-1", po_price=100.0, sap_base_price=100.0,
            event_type="EMAIL_ORDER_ENTRY_REQUEST",
        )
        origin, channel = derive_origin_and_channel(event)
        assert origin == "CUSTOMER"
        assert channel == "email"

    def test_edi_event_type_routes_to_api(self):
        event = OrderEvent(
            order_id="SO-1", po_price=100.0, sap_base_price=100.0,
            event_type="EDI_850_DUPLICATE_PO",
        )
        origin, channel = derive_origin_and_channel(event)
        assert origin == "API"
        assert channel == "edi_x12_850"

    def test_unknown_event_type_defaults_to_api(self):
        event = OrderEvent(
            order_id="SO-1", po_price=100.0, sap_base_price=100.0,
            event_type="UNKNOWN_EVENT_TYPE",
        )
        origin, channel = derive_origin_and_channel(event)
        assert origin == "API"

    def test_explicit_metadata_overrides_inference(self):
        # Portal orders are API per requirements §3 glossary; metadata
        # carries origin='API' + source_channel='portal'.
        event = OrderEvent(
            order_id="SO-1", po_price=100.0, sap_base_price=100.0,
            event_type="GENERIC",
            metadata={"origin": "API", "source_channel": "portal"},
        )
        origin, channel = derive_origin_and_channel(event)
        assert origin == "API"
        assert channel == "portal"

    def test_partial_metadata_falls_back_to_inference(self):
        # Only one of origin/source_channel set → ignore both, infer.
        event = OrderEvent(
            order_id="SO-1", po_price=100.0, sap_base_price=100.0,
            event_type="EDI_850_DUPLICATE_PO",
            metadata={"origin": "API"},  # missing channel
        )
        _origin, channel = derive_origin_and_channel(event)
        # Inference path used; channel defaults to edi_x12_850.
        assert channel == "edi_x12_850"


# ---------------------------------------------------------------------------
# should_materialise — case open/skip policy
# ---------------------------------------------------------------------------


class TestShouldMaterialise:
    def test_manual_order_always_materialises(self):
        event = OrderEvent(
            order_id="EML-1", po_price=100.0, sap_base_price=100.0,
            event_type="EMAIL_ORDER_ENTRY_REQUEST",
        )
        # Even on COMPLETE, Manual Orders open a case (eager).
        assert should_materialise(event, "COMPLETE") is True
        assert should_materialise(event, "MANUAL_REVIEW_REQUIRED") is True
        assert should_materialise(event, None) is True

    def test_automated_complete_materialises_post_s15a(self):
        """S15a amendment 2026-05-12 — every persisted record gets a case.

        Pre-amendment, clean Automated COMPLETE bypassed case
        materialisation (Tier-1 stateless path, parent_case_id=None).
        Post-amendment the asoe-ui case-centric pivot retired the
        /exceptions/[id] surface; without a case there's nowhere for
        these records to live. Policy widened: clean COMPLETE
        Automated records now also open a case (essentially
        "already resolved" — operators don't need to act, but the
        audit surface and bookmarkable /cases/{id} URL are present).
        """
        event = OrderEvent(
            order_id="SO-1", po_price=100.0, sap_base_price=100.0,
            event_type="EDI_850_DUPLICATE_PO",
        )
        assert should_materialise(event, "COMPLETE") is True

    def test_automated_non_clean_terminals_materialise(self):
        event = OrderEvent(
            order_id="SO-1", po_price=100.0, sap_base_price=100.0,
            event_type="EDI_850_DUPLICATE_PO",
        )
        for status in (
            "MANUAL_REVIEW_REQUIRED", "BLOCKED",
            "FAIL_TO_HUMAN", "REJECTED", "AUDIT_CONTEXT_MISSING",
        ):
            assert should_materialise(event, status) is True, (
                f"Automated {status} must materialise a case"
            )

    def test_automated_unknown_status_materialises_post_s15a(self):
        """S15a amendment — every persisted record gets a case.

        Pre-amendment, unknown / None status on Automated bypassed
        case open. Post-amendment everything materialises (see the
        section comment in ``api/case_resolver.py``).
        """
        event = OrderEvent(
            order_id="SO-1", po_price=100.0, sap_base_price=100.0,
            event_type="EDI_850_DUPLICATE_PO",
        )
        assert should_materialise(event, None) is True
        assert should_materialise(event, "EXOTIC_STATE") is True


# ---------------------------------------------------------------------------
# materialise_for_event — full end-to-end resolution
# ---------------------------------------------------------------------------


class TestMaterialiseForEvent:
    def test_clean_automated_opens_case_post_s15a(self):
        # S15a amendment 2026-05-12 — every persisted record gets a
        # case, including clean-COMPLETE Automated Orders. Pre-
        # amendment this returned None and persisted nothing.
        event = OrderEvent(
            order_id="SO-1", po_price=100.0, sap_base_price=100.0,
            event_type="EDI_850_DUPLICATE_PO",
            retailer_id="R-100",
        )
        case = materialise_for_event("t1", event, "COMPLETE")
        assert case is not None
        assert case.origin == "API"
        assert len(case_store.list_by_tenant("t1")) == 1

    def test_non_clean_automated_opens_case(self):
        event = OrderEvent(
            order_id="SO-1", po_price=100.0, sap_base_price=100.0,
            event_type="EDI_850_DUPLICATE_PO",
            retailer_id="R-100",
        )
        case = materialise_for_event("t1", event, "MANUAL_REVIEW_REQUIRED")
        assert case is not None
        assert case.origin == "API"
        assert case.source_channel == "edi_x12_850"
        assert case.customer_po_number == "SO-1"
        assert case.customer_id == "R-100"

    def test_manual_order_eager_materialisation(self):
        event = OrderEvent(
            order_id="EML-1", po_price=0.0, sap_base_price=0.0,
            event_type="EMAIL_ORDER_ENTRY_REQUEST",
            retailer_id="R-200",
        )
        # Manual Orders open even on COMPLETE.
        case = materialise_for_event("t1", event, "COMPLETE")
        assert case is not None
        assert case.origin == "CUSTOMER"
        assert case.source_channel == "email"

    def test_multiple_events_same_po_attach_to_one_case(self):
        """ADR-038 §6.2 — same customer_po across events resolves to
        the same case via the correlation table."""
        event_1 = OrderEvent(
            order_id="PO-7777", po_price=100.0, sap_base_price=100.0,
            event_type="EDI_850_DUPLICATE_PO",
            retailer_id="R-100",
        )
        case_1 = materialise_for_event("t1", event_1, "MANUAL_REVIEW_REQUIRED")

        # Second non-clean event for the same PO — should attach.
        event_2 = OrderEvent(
            order_id="PO-7777", po_price=100.0, sap_base_price=100.0,
            event_type="EDI_850_LINE_MISMATCH",
            retailer_id="R-100",
        )
        case_2 = materialise_for_event("t1", event_2, "BLOCKED")

        assert case_1 is not None and case_2 is not None
        assert case_1.case_id == case_2.case_id
        # Only ONE case persisted across the two events.
        assert len(case_store.list_by_tenant("t1")) == 1

    def test_explicit_source_channel_propagates(self):
        # Portal-typed order: metadata overrides inference; origin is
        # API (system-initiated) and channel is 'portal'.
        event = OrderEvent(
            order_id="PORTAL-9", po_price=100.0, sap_base_price=100.0,
            event_type="ORDER_RECEIVED",
            metadata={
                "origin": "API",
                "source_channel": "portal",
            },
        )
        case = materialise_for_event("t1", event, "MANUAL_REVIEW_REQUIRED")
        assert case is not None
        assert case.origin == "API"
        assert case.source_channel == "portal"

    def test_correlation_keys_from_metadata(self):
        # Metadata carries explicit correlation identifiers.
        event = OrderEvent(
            order_id="ANY", po_price=100.0, sap_base_price=100.0,
            event_type="EDI_850_DUPLICATE_PO",
            metadata={
                "customer_po_number": "PO-1234",
                "sales_order_id": "SO-9988",
                "edi_transaction_id": "EDI-T-001",
            },
        )
        case = materialise_for_event("t1", event, "MANUAL_REVIEW_REQUIRED")
        assert case is not None
        assert case.customer_po_number == "PO-1234"
        assert case.sales_order_id == "SO-9988"
        assert case.edi_transaction_id == "EDI-T-001"

    def test_bundle_version_stamped_on_case_open(self):
        event = OrderEvent(
            order_id="SO-1", po_price=100.0, sap_base_price=100.0,
            event_type="EDI_850_DUPLICATE_PO",
        )
        case = materialise_for_event(
            "t1", event, "MANUAL_REVIEW_REQUIRED",
            bundle_version_at_open="duplicate-po@1.0.0",
        )
        assert case.bundle_version_at_open == "duplicate-po@1.0.0"
