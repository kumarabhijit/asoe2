"""Phase 3 — CaseStore classification history (in-memory mirror of V020).

Acceptance criterion #9: every classification event produces exactly
one ``ClassificationEvent`` row stamped with the taxonomy version in
effect; the history is append-only.

Authority: docs/specs/case-intent-supergroup-requirements.md §8.6.
"""

from __future__ import annotations

import pytest

from api.store import CaseStore
from contracts._generated.taxonomy_constants import TAXONOMY_VERSION
from contracts.models import ClassificationEvent


@pytest.fixture
def store() -> CaseStore:
    return CaseStore()


def _open(store: CaseStore, *, supergroup_code: str | None = "SG_NEW_ORDER") -> str:
    """Helper — opens a CUSTOMER-origin case. By default carries a
    supergroup_code at intake (the realistic path; auto-writes one
    history row). Pass ``supergroup_code=None`` to open without
    classification."""
    case, _ = store.lookup_or_create(
        tenant_id="t1", origin="CUSTOMER", source_channel="email",
        customer_po_number="PO-1", supergroup_code=supergroup_code,
    )
    return case.case_id


# ---------------------------------------------------------------------------
# Record + read
# ---------------------------------------------------------------------------

def test_record_returns_event(store: CaseStore):
    case_id = _open(store)
    event = store.record_classification(
        case_id,
        supergroup_code="SG_NEW_ORDER",
        intent_code="INT_MANUAL_ORDER_INTAKE",
        classified_by="user:csr-1",
        classifier_type="HUMAN",
    )
    assert isinstance(event, ClassificationEvent)
    assert event.case_id == case_id
    assert event.supergroup_code == "SG_NEW_ORDER"
    assert event.intent_code == "INT_MANUAL_ORDER_INTAKE"
    assert event.classifier_type == "HUMAN"


def test_history_returns_empty_for_unclassified_case(store: CaseStore):
    """An intake without a supergroup_code does not auto-write a row."""
    case_id = _open(store, supergroup_code=None)
    assert store.get_classification_history(case_id) == []


def test_record_appends_to_history(store: CaseStore):
    case_id = _open(store, supergroup_code=None)
    store.record_classification(
        case_id, supergroup_code="SG_NEW_ORDER",
        intent_code="INT_MANUAL_ORDER_INTAKE",
        classified_by="user:csr-1", classifier_type="HUMAN",
    )
    history = store.get_classification_history(case_id)
    assert len(history) == 1
    assert history[0].supergroup_code == "SG_NEW_ORDER"


def test_reclassification_appends_not_overwrites(store: CaseStore):
    """Acceptance criterion #9 — every reclassification is a new row."""
    case_id = _open(store, supergroup_code=None)
    store.record_classification(
        case_id, supergroup_code="SG_NEW_ORDER",
        intent_code="INT_MANUAL_ORDER_INTAKE",
        classified_by="user:csr-1", classifier_type="HUMAN",
    )
    store.record_classification(
        case_id, supergroup_code="SG_NEEDS_TRIAGE",
        intent_code="INT_UNKNOWN",
        classified_by="user:lead-1", classifier_type="HUMAN",
        reason_text="CSR misclassified; needs analyst review",
    )
    history = store.get_classification_history(case_id)
    assert len(history) == 2
    assert history[0].supergroup_code == "SG_NEW_ORDER"
    assert history[1].supergroup_code == "SG_NEEDS_TRIAGE"


def test_taxonomy_version_stamped(store: CaseStore):
    case_id = _open(store)
    event = store.record_classification(
        case_id, supergroup_code="SG_NEW_ORDER",
        intent_code="INT_MANUAL_ORDER_INTAKE",
        classified_by="user:csr-1", classifier_type="HUMAN",
    )
    assert event.taxonomy_version == TAXONOMY_VERSION


def test_unknown_case_raises(store: CaseStore):
    with pytest.raises(KeyError, match="Unknown case_id"):
        store.record_classification(
            "non-existent-case-id",
            supergroup_code="SG_NEW_ORDER",
            intent_code="INT_MANUAL_ORDER_INTAKE",
            classified_by="user:csr-1", classifier_type="HUMAN",
        )


# ---------------------------------------------------------------------------
# Classifier types (requirements §8.3 matrix)
# ---------------------------------------------------------------------------

def test_human_classifier_type_accepted(store: CaseStore):
    case_id = _open(store, supergroup_code=None)
    e = store.record_classification(
        case_id, supergroup_code="SG_NEW_ORDER",
        classified_by="user:csr-1", classifier_type="HUMAN",
    )
    assert e.classifier_type == "HUMAN"


def test_model_classifier_carries_model_version(store: CaseStore):
    case_id = _open(store, supergroup_code=None)
    e = store.record_classification(
        case_id, supergroup_code="SG_NEW_ORDER",
        intent_code="INT_MANUAL_ORDER_INTAKE",
        classified_by="system:intent_classifier",
        classifier_type="MODEL",
        model_version="2026-05-01-claude-haiku-4-5",
    )
    assert e.classifier_type == "MODEL"
    assert e.model_version == "2026-05-01-claude-haiku-4-5"


def test_rule_classifier_for_unmapped_block_code(store: CaseStore):
    """SAP block code with no mapping routes to SG_BLOCK_UNMAPPED via the
    deterministic RULE path; the history row records that."""
    case, _ = store.lookup_or_create(
        tenant_id="t1", origin="API", source_channel="edi_x12_850",
        sales_order_id="SO-RULE-1", supergroup_code="SG_BLOCK_UNMAPPED",
    )
    e = store.record_classification(
        case.case_id, supergroup_code="SG_BLOCK_UNMAPPED",
        intent_code="INT_UNMAPPED_PENDING_TAXONOMY",
        classified_by="system:sap_block_router", classifier_type="RULE",
        reason_text="SAP block code 'ZZ' not in case_intent.sap_block_code",
    )
    assert e.classifier_type == "RULE"


def test_invalid_classifier_type_rejected(store: CaseStore):
    case_id = _open(store)
    with pytest.raises(Exception):  # ValidationError
        store.record_classification(
            case_id, supergroup_code="SG_NEW_ORDER",
            classified_by="user:csr-1", classifier_type="BOT",
        )


# ---------------------------------------------------------------------------
# Append-only semantics
# ---------------------------------------------------------------------------

def test_returned_history_list_is_a_copy(store: CaseStore):
    """Callers cannot mutate the in-memory store by mutating the
    returned list."""
    case_id = _open(store, supergroup_code=None)
    store.record_classification(
        case_id, supergroup_code="SG_NEW_ORDER",
        intent_code="INT_MANUAL_ORDER_INTAKE",
        classified_by="user:csr-1", classifier_type="HUMAN",
    )
    history = store.get_classification_history(case_id)
    history.clear()  # mutate the caller's copy
    assert len(store.get_classification_history(case_id)) == 1


def test_event_model_rejects_extra_fields():
    """ClassificationEvent ships with extra='forbid' — typos at the
    construction site fail loudly."""
    with pytest.raises(Exception):  # ValidationError
        ClassificationEvent(  # type: ignore[call-arg]
            case_id="c1", supergroup_code="SG_NEW_ORDER",
            classified_by="u", classifier_type="HUMAN",
            taxonomy_version="v",
            mystery_field="surprise",
        )


def test_child_case_id_optional_for_parent_level_events(store: CaseStore):
    """A reclassification of the case itself (no concurrent child
    relabel) leaves ``child_case_id`` as None."""
    case_id = _open(store, supergroup_code=None)
    e = store.record_classification(
        case_id, supergroup_code="SG_ORDER_CHANGE",
        classified_by="user:lead-1", classifier_type="HUMAN",
    )
    assert e.child_case_id is None


# ---------------------------------------------------------------------------
# Auto-recording on intake (lookup_or_create) and update
# ---------------------------------------------------------------------------

def test_lookup_or_create_writes_intake_event_when_supergroup_set(
    store: CaseStore,
):
    """Acceptance criterion #9 at the intake boundary — opening a case
    with a supergroup_code appends one ClassificationEvent. Without an
    explicit classifier, defaults are ``RULE`` / ``system:case_intake``
    (the deterministic SAP-block-code mapping is the typical intake)."""
    case, opened = store.lookup_or_create(
        tenant_id="t1", origin="API", source_channel="edi_x12_850",
        sales_order_id="SO-AUTO-1",
        supergroup_code="SG_BLOCK_PRICING",
        intent_code="INT_PRICE_MISMATCH",
    )
    assert opened is True
    history = store.get_classification_history(case.case_id)
    assert len(history) == 1
    assert history[0].supergroup_code == "SG_BLOCK_PRICING"
    assert history[0].intent_code == "INT_PRICE_MISMATCH"
    assert history[0].classifier_type == "RULE"
    assert history[0].classified_by == "system:case_intake"


def test_lookup_or_create_respects_explicit_classifier(store: CaseStore):
    """A CUSTOMER intake that ran a MODEL classifier upstream passes
    its own classifier_type and classified_by; the audit row reflects
    that, not the RULE default."""
    case, _ = store.lookup_or_create(
        tenant_id="t1", origin="CUSTOMER", source_channel="email",
        customer_po_number="PO-MODEL-1",
        supergroup_code="SG_NEW_ORDER",
        intent_code="INT_MANUAL_ORDER_INTAKE",
        classified_by="system:email_classifier",
        classifier_type="MODEL",
    )
    history = store.get_classification_history(case.case_id)
    assert history[0].classifier_type == "MODEL"
    assert history[0].classified_by == "system:email_classifier"


def test_lookup_or_create_no_supergroup_no_event(store: CaseStore):
    """Opening without supergroup_code (pre-classifier) does not write
    a placeholder row — the audit captures classifications, not
    case-creations."""
    case, _ = store.lookup_or_create(
        tenant_id="t1", origin="API", source_channel="edi_x12_850",
        sales_order_id="SO-NO-SG-1",
    )
    assert store.get_classification_history(case.case_id) == []


def test_update_supergroup_writes_event(store: CaseStore):
    """Reclassification via update() appends one history row atomically
    with the state mutation."""
    case_id = _open(store)
    history_before = len(store.get_classification_history(case_id))
    store.update(
        case_id, supergroup_code="SG_ORDER_CHANGE",
        classified_by="user:csr-1", classifier_type="HUMAN",
        reason_text="Customer clarified — was an order change",
    )
    history_after = store.get_classification_history(case_id)
    assert len(history_after) == history_before + 1
    new_event = history_after[-1]
    assert new_event.supergroup_code == "SG_ORDER_CHANGE"
    assert new_event.classifier_type == "HUMAN"
    assert new_event.reason_text == "Customer clarified — was an order change"


def test_update_supergroup_to_same_value_no_event(store: CaseStore):
    """An update() with supergroup_code unchanged is not a
    reclassification — no audit row appended."""
    case_id = _open(store)  # opens with SG_NEW_ORDER (one row written)
    history_before = len(store.get_classification_history(case_id))
    store.update(
        case_id, supergroup_code="SG_NEW_ORDER",
        classified_by="user:csr-1", classifier_type="HUMAN",
    )
    assert len(store.get_classification_history(case_id)) == history_before


def test_update_without_supergroup_no_classifier_needed(store: CaseStore):
    """Updates that don't touch supergroup_code stay simple — no
    audit kwargs required."""
    case_id = _open(store)
    store.update(case_id, status="OPEN_AWAITING_HUMAN")  # no classifier kwargs
    # History from intake remains; no new row appended.
    assert len(store.get_classification_history(case_id)) == 1
