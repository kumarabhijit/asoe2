"""Supergroup classification at case intake (B1/B2).

A case used to open with ``supergroup_code = None`` — the classifier
that fills it in was unwired ("Phase 5 will fill those in"). The result
on real/Azure traffic: blank supergroup chips and an empty
classification-history strip, even though the asoe-ui mock fabricated a
supergroup. This wires the deterministic RULE classification: the case's
supergroup is resolved from the child's classified intent via the
governed taxonomy SoT (`contracts/taxonomy.supergroup_for_intent`) and
recorded in `case_classification_history` at open.

The matching asoe-ui lock is
`tests/architectural/mock_supergroup_taxonomy_parity.test.ts`.
"""

from __future__ import annotations

import pytest

from api.case_resolver import materialise_for_event
from api.store import case_store
from contracts.models import Intent, OrderEvent
from contracts.taxonomy import intent_code_for, supergroup_for_intent


# ---------------------------------------------------------------------------
# Unit: the taxonomy-derived resolver
# ---------------------------------------------------------------------------


def test_supergroup_for_intent_resolves_every_intent():
    """Every member of the Intent enum maps to a supergroup via the
    governed taxonomy — the INT_ prefix convention is total, so no
    classified intent is left unmapped."""
    for member in Intent:
        assert supergroup_for_intent(member.value) is not None, member.value


def test_contractual_correction_is_order_integrity_not_pricing():
    """The exact mock-vs-taxonomy disagreement the expert panel ruled
    on: CONTRACTUAL_CORRECTION is order-integrity, not pricing."""
    assert supergroup_for_intent("CONTRACTUAL_CORRECTION") == "SG_BLOCK_ORDER_INTEGRITY"


@pytest.mark.parametrize(
    "intent,expected",
    [
        ("DUPLICATE_PO", "SG_BLOCK_ORDER_INTEGRITY"),
        ("EDI_MISMATCH", "SG_BLOCK_ORDER_INTEGRITY"),
        ("PRICE_HOLD_RELEASE", "SG_BLOCK_PRICING"),
        ("MASS_PRICING_ERROR", "SG_BLOCK_PRICING"),
        ("CREDIT_BLOCK", "SG_BLOCK_CREDIT"),
        ("BACK_ORDER", "SG_BLOCK_AVAILABILITY"),
        ("MANUAL_ORDER_INTAKE", "SG_NEW_ORDER"),
        ("UNKNOWN", "SG_NEEDS_TRIAGE"),
    ],
)
def test_supergroup_for_intent_spot_checks(intent, expected):
    assert supergroup_for_intent(intent) == expected


def test_supergroup_for_intent_none_and_unmapped():
    assert supergroup_for_intent(None) is None
    assert supergroup_for_intent("") is None
    # An intent with no taxonomy mapping → None (never a guessed sentinel).
    assert supergroup_for_intent("NOT_A_REAL_INTENT") is None


def test_intent_code_for_prefix_convention():
    assert intent_code_for("DUPLICATE_PO") == "INT_DUPLICATE_PO"
    # Already-prefixed passes through unchanged (idempotent).
    assert intent_code_for("INT_DUPLICATE_PO") == "INT_DUPLICATE_PO"


# ---------------------------------------------------------------------------
# Integration: intake wiring through materialise_for_event
# ---------------------------------------------------------------------------


def _edi_event(order_id: str) -> OrderEvent:
    return OrderEvent(
        order_id=order_id,
        event_type="EDI_850_LINE_MISMATCH",
        po_price=100.0,
        sap_base_price=120.0,
    )


def test_case_opens_with_taxonomy_supergroup_and_history():
    event = _edi_event("PO-SG-1")
    case = materialise_for_event(
        "tenant-sg", event, final_status="FAIL_TO_HUMAN",
        intent="CONTRACTUAL_CORRECTION",
    )
    assert case is not None
    # B1: the supergroup chip is populated — and correct per the taxonomy.
    assert case.supergroup_code == "SG_BLOCK_ORDER_INTEGRITY"

    # B2: exactly one RULE classification event is recorded at open.
    history = case_store.get_classification_history(case.case_id)
    assert len(history) == 1
    ev = history[0]
    assert ev.supergroup_code == "SG_BLOCK_ORDER_INTEGRITY"
    assert ev.classifier_type == "RULE"
    assert ev.classified_by == "system:case_intake"


def test_case_without_intent_opens_without_supergroup():
    """Absent intent → supergroup stays unset (unchanged behaviour); no
    classification event is fabricated."""
    event = _edi_event("PO-SG-2")
    case = materialise_for_event(
        "tenant-sg2", event, final_status="FAIL_TO_HUMAN", intent=None,
    )
    assert case is not None
    assert case.supergroup_code is None
    assert case_store.get_classification_history(case.case_id) == []


def test_attach_keeps_first_supergroup():
    """A second event attaching to the same case does not re-classify;
    the supergroup set at open is retained."""
    e1 = _edi_event("PO-SG-3")
    case1 = materialise_for_event(
        "tenant-sg3", e1, final_status="FAIL_TO_HUMAN",
        intent="DUPLICATE_PO",
    )
    assert case1.supergroup_code == "SG_BLOCK_ORDER_INTEGRITY"

    # Same PO → same case; a differently-classified child must not flip it.
    e2 = _edi_event("PO-SG-3")
    case2 = materialise_for_event(
        "tenant-sg3", e2, final_status="FAIL_TO_HUMAN",
        intent="CREDIT_BLOCK",
    )
    assert case2.case_id == case1.case_id
    assert case2.supergroup_code == "SG_BLOCK_ORDER_INTEGRITY"
