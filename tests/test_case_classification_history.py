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


def _open(store: CaseStore) -> str:
    case, _ = store.lookup_or_create(
        tenant_id="t1", origin="CUSTOMER", source_channel="email",
        customer_po_number="PO-1", supergroup_code="SG_NEW_ORDER",
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
    case_id = _open(store)
    assert store.get_classification_history(case_id) == []


def test_record_appends_to_history(store: CaseStore):
    case_id = _open(store)
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
    case_id = _open(store)
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
    case_id = _open(store)
    e = store.record_classification(
        case_id, supergroup_code="SG_NEW_ORDER",
        classified_by="user:csr-1", classifier_type="HUMAN",
    )
    assert e.classifier_type == "HUMAN"


def test_model_classifier_carries_model_version(store: CaseStore):
    case_id = _open(store)
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
    case_id = _open(store)
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
    case_id = _open(store)
    e = store.record_classification(
        case_id, supergroup_code="SG_ORDER_CHANGE",
        classified_by="user:lead-1", classifier_type="HUMAN",
    )
    assert e.child_case_id is None
