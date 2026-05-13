"""ADR-041 §3 — case_type / email_classification / sap_block_code locks.

These tests pin the additive case-typing axis the PO requested:

  * Every OrderCase carries a `case_type` of EMAIL_ENTRY or BLOCK
    (orthogonal to `source` per the domain modeller's pushback —
    `manual_order/automated_order` describes how the order originated;
    `EMAIL_ENTRY/BLOCK` describes why ASOE materialised the case).
  * EMAIL_ENTRY ⇒ `email_classification` populated (NEW_ORDER |
    ORDER_CHANGE | INQUIRY | COMPLAINT | OTHER), 1:1 with the intake.
  * BLOCK ⇒ `email_classification` strictly None; SAP block-reason
    codes live on each child ExceptionRecord as `sap_block_code`
    (1:N — one SAP order can carry multiple block codes).

Locks the back-compat default so call sites that only know
`source`/`channel` don't have to compute case_type themselves.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.store import ExceptionRecord, case_store
from contracts.models import OrderCase, infer_case_type


@pytest.fixture(autouse=True)
def _reset_case_store():
    case_store.clear()
    yield
    case_store.clear()


# ---------------------------------------------------------------------------
# infer_case_type — back-compat default
# ---------------------------------------------------------------------------


class TestInferCaseType:
    def test_manual_order_email_defaults_to_email_entry(self):
        assert infer_case_type("manual_order", "email") == "EMAIL_ENTRY"

    def test_automated_edi_defaults_to_block(self):
        assert infer_case_type("automated_order", "edi_x12_850") == "BLOCK"

    def test_automated_portal_defaults_to_block(self):
        assert infer_case_type("automated_order", "portal") == "BLOCK"


# ---------------------------------------------------------------------------
# OrderCase — case_type inference + email_classification defaulting
# ---------------------------------------------------------------------------


class TestOrderCaseDefaults:
    def test_manual_order_infers_email_entry_and_defaults_classification(self):
        case = OrderCase(
            tenant_id="t1",
            source="manual_order",
            source_channel="email",
            source_email_id="msg-001",
        )
        assert case.case_type == "EMAIL_ENTRY"
        # The back-compat default is OTHER — the eventual ADR-041
        # classification graph node sets a specific value.
        assert case.email_classification == "OTHER"

    def test_automated_order_infers_block_and_forces_classification_none(self):
        case = OrderCase(
            tenant_id="t1",
            source="automated_order",
            source_channel="edi_x12_850",
            sales_order_id="SO-1001",
        )
        assert case.case_type == "BLOCK"
        assert case.email_classification is None

    def test_explicit_case_type_wins_over_inference(self):
        # source=manual_order would default to EMAIL_ENTRY; passing
        # case_type=BLOCK explicitly takes precedence (rare but legal
        # — e.g. an email-channel ingestion path that downgraded to a
        # block resolution shape during routing).
        case = OrderCase(
            tenant_id="t1",
            source="manual_order",
            source_channel="email",
            sales_order_id="SO-9001",
            case_type="BLOCK",
        )
        assert case.case_type == "BLOCK"
        assert case.email_classification is None


# ---------------------------------------------------------------------------
# Invariant locks — the validator catches conflated axes
# ---------------------------------------------------------------------------


class TestCaseTypeInvariants:
    def test_block_with_email_classification_is_force_stripped(self):
        # The mode="before" validator strips the bad classification
        # rather than fail — passing both is a conflated-axes signal
        # the modeller flagged as a "common PO mistake".
        case = OrderCase(
            tenant_id="t1",
            source="automated_order",
            source_channel="edi_x12_850",
            sales_order_id="SO-1001",
            case_type="BLOCK",
            email_classification="NEW_ORDER",
        )
        assert case.email_classification is None

    def test_email_entry_with_explicit_classification_passes(self):
        case = OrderCase(
            tenant_id="t1",
            source="manual_order",
            source_channel="email",
            source_email_id="msg-001",
            case_type="EMAIL_ENTRY",
            email_classification="ORDER_CHANGE",
        )
        assert case.case_type == "EMAIL_ENTRY"
        assert case.email_classification == "ORDER_CHANGE"

    def test_email_entry_rejects_invalid_classification(self):
        with pytest.raises(ValidationError):
            OrderCase(
                tenant_id="t1",
                source="manual_order",
                source_channel="email",
                source_email_id="msg-001",
                case_type="EMAIL_ENTRY",
                email_classification="FOLLOW_UP",  # not in the literal
            )

    def test_case_type_rejects_unknown_value(self):
        with pytest.raises(ValidationError):
            OrderCase(
                tenant_id="t1",
                source="manual_order",
                source_channel="email",
                case_type="ESCALATION",  # not in the literal
            )


# ---------------------------------------------------------------------------
# ExceptionRecord — sap_block_code is wired
# ---------------------------------------------------------------------------


class TestExceptionRecordSapBlockCode:
    def test_sap_block_code_defaults_none(self):
        rec = ExceptionRecord(
            tenant_id="t1",
            order_id="SO-1001",
            event_type="EDI_850_PRICE_MISMATCH",
            trace_id="trace-001",
        )
        assert rec.sap_block_code is None

    def test_sap_block_code_accepts_value(self):
        rec = ExceptionRecord(
            tenant_id="t1",
            order_id="SO-1001",
            event_type="EDI_850_PRICE_MISMATCH",
            trace_id="trace-001",
            sap_block_code="ZPRC",  # SAP price-block reason code
        )
        assert rec.sap_block_code == "ZPRC"


# ---------------------------------------------------------------------------
# CaseStore.lookup_or_create — derivation + caller override both work
# ---------------------------------------------------------------------------


class TestLookupOrCreateCaseType:
    def test_manual_event_opens_email_entry_case(self):
        case, opened = case_store.lookup_or_create(
            tenant_id="t1",
            source="manual_order",
            source_channel="email",
            source_email_id="msg-001",
        )
        assert opened
        assert case.case_type == "EMAIL_ENTRY"
        assert case.email_classification == "OTHER"  # default

    def test_automated_event_opens_block_case(self):
        case, opened = case_store.lookup_or_create(
            tenant_id="t1",
            source="automated_order",
            source_channel="edi_x12_850",
            sales_order_id="SO-1001",
        )
        assert opened
        assert case.case_type == "BLOCK"
        assert case.email_classification is None

    def test_explicit_email_classification_overrides_default(self):
        case, _ = case_store.lookup_or_create(
            tenant_id="t1",
            source="manual_order",
            source_channel="email",
            source_email_id="msg-002",
            email_classification="COMPLAINT",
        )
        assert case.case_type == "EMAIL_ENTRY"
        assert case.email_classification == "COMPLAINT"
