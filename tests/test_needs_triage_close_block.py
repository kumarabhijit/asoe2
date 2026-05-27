"""Phase 3 — NEEDS_TRIAGE hard-block at close (requirements §8.2 #1).

A case whose ``supergroup_code == 'SG_NEEDS_TRIAGE'`` cannot transition
to ``RESOLVED``. Callers must reclassify the case to a real super-group
first. Acceptance criterion #5.
"""

from __future__ import annotations

import pytest

from api.store import CaseStore, NeedsTriageCloseBlocked


@pytest.fixture
def store() -> CaseStore:
    return CaseStore()


def _open(store: CaseStore, *, supergroup_code: str = "SG_NEW_ORDER",
          origin: str = "CUSTOMER") -> str:
    case, _ = store.lookup_or_create(
        tenant_id="t1", origin=origin, source_channel="email",
        customer_po_number="PO-1", supergroup_code=supergroup_code,
    )
    return case.case_id


def test_resolve_blocked_for_needs_triage(store: CaseStore):
    case_id = _open(store, supergroup_code="SG_NEEDS_TRIAGE")
    with pytest.raises(NeedsTriageCloseBlocked, match="SG_NEEDS_TRIAGE"):
        store.update(case_id, status="RESOLVED")


def test_needs_triage_close_blocked_is_not_a_value_error(store: CaseStore):
    """NeedsTriageCloseBlocked inherits from Exception, not ValueError —
    callers must handle it explicitly. CLAUDE.md §5 (failures explicit)."""
    case_id = _open(store, supergroup_code="SG_NEEDS_TRIAGE")
    with pytest.raises(NeedsTriageCloseBlocked):
        store.update(case_id, status="RESOLVED")
    assert not issubclass(NeedsTriageCloseBlocked, ValueError)


def test_reclassify_then_resolve_succeeds(store: CaseStore):
    """The escape hatch: reclassify to a real super-group, then close."""
    case_id = _open(store, supergroup_code="SG_NEEDS_TRIAGE")
    store.update(
        case_id, supergroup_code="SG_NEW_ORDER",
        classified_by="user:lead-1", classifier_type="HUMAN",
        reason_text="Triage complete — order intake",
    )
    case = store.update(case_id, status="RESOLVED")
    assert case.status == "RESOLVED"
    assert case.supergroup_code == "SG_NEW_ORDER"


def test_reclassify_into_needs_triage_still_blocks_resolve(store: CaseStore):
    case_id = _open(store, supergroup_code="SG_NEW_ORDER")
    store.update(
        case_id, supergroup_code="SG_NEEDS_TRIAGE",
        classified_by="user:lead-1", classifier_type="HUMAN",
        reason_text="Cannot classify — escalate",
    )
    with pytest.raises(NeedsTriageCloseBlocked):
        store.update(case_id, status="RESOLVED")


def test_non_resolve_transition_unaffected(store: CaseStore):
    """The block targets only RESOLVED; other transitions stay open
    so a CSR can park a NEEDS_TRIAGE case in OPEN_AWAITING_HUMAN."""
    case_id = _open(store, supergroup_code="SG_NEEDS_TRIAGE")
    case = store.update(case_id, status="OPEN_AWAITING_HUMAN")
    assert case.status == "OPEN_AWAITING_HUMAN"


def test_non_needs_triage_resolve_unaffected(store: CaseStore):
    case_id = _open(store, supergroup_code="SG_NEW_ORDER")
    case = store.update(case_id, status="RESOLVED")
    assert case.status == "RESOLVED"


def test_atomic_resolve_with_reclassify_in_one_update(store: CaseStore):
    """A single update that both reclassifies AND resolves is accepted —
    the resulting state is non-NEEDS_TRIAGE + RESOLVED. The reclassify
    half writes an audit row; the status change does not."""
    case_id = _open(store, supergroup_code="SG_NEEDS_TRIAGE")
    case = store.update(
        case_id,
        supergroup_code="SG_NEW_ORDER",
        status="RESOLVED",
        classified_by="user:lead-1", classifier_type="HUMAN",
        reason_text="Triage complete in same action as close",
    )
    assert case.supergroup_code == "SG_NEW_ORDER"
    assert case.status == "RESOLVED"


def test_update_requires_classifier_metadata_when_changing_supergroup(
    store: CaseStore,
):
    """Audit trail invariant — every SG change must be attributable."""
    case_id = _open(store, supergroup_code="SG_NEEDS_TRIAGE")
    with pytest.raises(ValueError, match="classified_by"):
        store.update(case_id, supergroup_code="SG_NEW_ORDER")
