"""ADR-036 — customer-email supergroup classifier (Phase 1 bootstrap).

Covers the constrained-output vocabulary lock, the deterministic backend,
the confidence-threshold sink, and the intake wiring through
resolve_or_open_case for CUSTOMER-origin emails.
"""

from __future__ import annotations

from typing import get_args

import pytest

from constraints.specs import AllowedCustomerSupergroup, EmailSupergroupDecision
from contracts._generated.taxonomy_constants import SUPERGROUPS_BY_ORIGIN
from contracts.models import GraphState, OrderEvent
from api.email_supergroup_classifier import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    EMAIL_CATEGORY_HINT_KEY,
    DeterministicEmailSupergroupBackend,
    EmailSupergroupClassifier,
)


# ---------------------------------------------------------------------------
# Constrained-output vocabulary lock (CLAUDE.md §3 / ADR-036 D1)
# ---------------------------------------------------------------------------


def test_allowed_customer_supergroup_matches_taxonomy_sot():
    """AllowedCustomerSupergroup (hand-maintained Literal) must equal the
    generated SUPERGROUPS_BY_ORIGIN['CUSTOMER'] so the classifier vocab
    cannot drift from the governed taxonomy."""
    literal = set(get_args(AllowedCustomerSupergroup))
    taxonomy = set(SUPERGROUPS_BY_ORIGIN["CUSTOMER"])
    assert literal == taxonomy, (
        "AllowedCustomerSupergroup is out of sync with the taxonomy "
        f"CUSTOMER supergroups. only-in-literal={literal - taxonomy}, "
        f"only-in-taxonomy={taxonomy - literal}"
    )


def test_decision_rejects_non_customer_supergroup():
    """The schema constrains supergroup_code to CUSTOMER codes — an API
    block supergroup must not validate."""
    with pytest.raises(Exception):
        EmailSupergroupDecision(supergroup_code="SG_BLOCK_PRICING", confidence=0.9)


# ---------------------------------------------------------------------------
# Deterministic backend + classifier
# ---------------------------------------------------------------------------


def _email_event(hint=None, order_id="EML-1") -> OrderEvent:
    meta = {}
    if hint is not None:
        meta[EMAIL_CATEGORY_HINT_KEY] = hint
    return OrderEvent(
        order_id=order_id,
        event_type="MANUAL_ORDER_INTAKE",
        po_price=100.0,
        sap_base_price=100.0,
        metadata=meta,
    )


@pytest.mark.parametrize(
    "hint",
    [
        "SG_ORDER_CHANGE",
        "SG_ORDER_STATUS_INQUIRY",
        "SG_COMPLAINT_SERVICE",
        "SG_DOCUMENTATION",
        "SG_SHIPMENT_DISCREPANCY",
        "SG_BILLING_DISPUTE",
    ],
)
def test_deterministic_backend_relays_valid_hint(hint):
    backend = DeterministicEmailSupergroupBackend()
    state = GraphState(event=_email_event(hint=hint), tenant_id="t")
    decision = backend.classify_email_supergroup(state)
    assert decision.supergroup_code == hint
    assert decision.confidence >= DEFAULT_CONFIDENCE_THRESHOLD


def test_deterministic_backend_unknown_hint_triages():
    backend = DeterministicEmailSupergroupBackend()
    state = GraphState(event=_email_event(hint="SG_NOT_REAL"), tenant_id="t")
    decision = backend.classify_email_supergroup(state)
    assert decision.supergroup_code == "SG_NEEDS_TRIAGE"
    assert decision.confidence < DEFAULT_CONFIDENCE_THRESHOLD


def test_deterministic_backend_missing_hint_triages():
    backend = DeterministicEmailSupergroupBackend()
    state = GraphState(event=_email_event(hint=None), tenant_id="t")
    decision = backend.classify_email_supergroup(state)
    assert decision.supergroup_code == "SG_NEEDS_TRIAGE"


def test_classifier_passes_through_high_confidence():
    clf = EmailSupergroupClassifier()
    state = GraphState(event=_email_event(hint="SG_ORDER_CHANGE"), tenant_id="t")
    assert clf.classify(state).supergroup_code == "SG_ORDER_CHANGE"


def test_classifier_collapses_below_threshold_to_triage():
    """A backend that returns a real supergroup but at low confidence must
    be collapsed to SG_NEEDS_TRIAGE — a low-confidence guess is never
    stamped (ADR-036 D4)."""

    class LowConfBackend:
        def classify_email_supergroup(self, state):
            return EmailSupergroupDecision(
                supergroup_code="SG_ORDER_CHANGE", confidence=0.50,
            )

    clf = EmailSupergroupClassifier(backend=LowConfBackend())
    state = GraphState(event=_email_event(), tenant_id="t")
    decision = clf.classify(state)
    assert decision.supergroup_code == "SG_NEEDS_TRIAGE"
    assert decision.confidence == 0.50  # preserved for the audit trail


def test_threshold_is_configurable():
    class MidConfBackend:
        def classify_email_supergroup(self, state):
            return EmailSupergroupDecision(
                supergroup_code="SG_DOCUMENTATION", confidence=0.70,
            )

    # Lower the bar — the same decision now passes through.
    clf = EmailSupergroupClassifier(backend=MidConfBackend(), confidence_threshold=0.6)
    state = GraphState(event=_email_event(), tenant_id="t")
    assert clf.classify(state).supergroup_code == "SG_DOCUMENTATION"


# ---------------------------------------------------------------------------
# Intake wiring through resolve_or_open_case
# ---------------------------------------------------------------------------


def _customer_email_event(hint, order_id) -> OrderEvent:
    # event_type in the manual/customer allowlist → origin=CUSTOMER.
    return OrderEvent(
        order_id=order_id,
        event_type="MANUAL_ORDER_INTAKE",
        po_price=100.0,
        sap_base_price=100.0,
        metadata={EMAIL_CATEGORY_HINT_KEY: hint},
    )


def test_customer_email_opens_with_classified_supergroup():
    from api.case_resolver import materialise_for_event
    from api.store import case_store

    event = _customer_email_event("SG_ORDER_CHANGE", "EML-OC-1")
    case = materialise_for_event("t-eml", event, final_status="MANUAL_REVIEW_REQUIRED")
    assert case is not None
    assert case.supergroup_code == "SG_ORDER_CHANGE"

    history = case_store.get_classification_history(case.case_id)
    assert len(history) == 1
    ev = history[0]
    assert ev.supergroup_code == "SG_ORDER_CHANGE"
    assert ev.classifier_type == "MODEL"
    assert ev.classified_by == "system:email_supergroup_classifier"


def test_customer_email_low_confidence_opens_unset():
    """No usable hint → classifier triages → supergroup stays unset (the
    case is a NEEDS_TRIAGE-style review item, not a fabricated label).

    We assert the supergroup is not a confidently-classified value; the
    intake leaves it None rather than stamping a guess."""
    from api.case_resolver import materialise_for_event

    event = _customer_email_event("SG_NOT_REAL", "EML-OC-2")
    case = materialise_for_event("t-eml2", event, final_status="MANUAL_REVIEW_REQUIRED")
    assert case is not None
    assert case.supergroup_code is None


def test_manual_order_intake_keeps_rule_new_order_path():
    """An un-hinted MANUAL_ORDER_INTAKE event still gets SG_NEW_ORDER via
    the RULE leaf path. The email classifier runs first on the CUSTOMER
    path but triages with no hint, so the leaf-derived SG_NEW_ORDER is the
    fallback — the un-hinted intake path is unchanged."""
    from api.case_resolver import materialise_for_event
    from api.store import case_store

    event = OrderEvent(
        order_id="EML-NO-1",
        event_type="MANUAL_ORDER_INTAKE",
        po_price=100.0,
        sap_base_price=100.0,
        metadata={},
    )
    case = materialise_for_event(
        "t-eml3", event, final_status="MANUAL_REVIEW_REQUIRED",
        intent="MANUAL_ORDER_INTAKE",
    )
    assert case is not None
    assert case.supergroup_code == "SG_NEW_ORDER"
    history = case_store.get_classification_history(case.case_id)
    assert history[0].classifier_type == "RULE"


def test_hinted_email_takes_precedence_over_leaf_new_order():
    """A confident email classification is authoritative for the CUSTOMER
    case supergroup (acceptance #1 / §8.5): a hinted SG_ORDER_CHANGE email
    that the intent classifier would call MANUAL_ORDER_INTAKE opens as
    SG_ORDER_CHANGE (MODEL), not SG_NEW_ORDER (RULE)."""
    from api.case_resolver import materialise_for_event
    from api.store import case_store

    event = _customer_email_event("SG_ORDER_CHANGE", "EML-PREC-1")
    case = materialise_for_event(
        "t-eml4", event, final_status="MANUAL_REVIEW_REQUIRED",
        intent="MANUAL_ORDER_INTAKE",  # what the intent classifier yields
    )
    assert case.supergroup_code == "SG_ORDER_CHANGE"
    assert case_store.get_classification_history(case.case_id)[0].classifier_type == "MODEL"
