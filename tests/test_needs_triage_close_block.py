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


def test_resolve_blocked_via_subclass_of_value_error(store: CaseStore):
    """The block is a ValueError subclass so callers that catch
    ValueError keep working."""
    case_id = _open(store, supergroup_code="SG_NEEDS_TRIAGE")
    with pytest.raises(ValueError):
        store.update(case_id, status="RESOLVED")


def test_reclassify_then_resolve_succeeds(store: CaseStore):
    """The escape hatch: reclassify to a real super-group, then close."""
    case_id = _open(store, supergroup_code="SG_NEEDS_TRIAGE")
    store.update(case_id, supergroup_code="SG_NEW_ORDER")
    case = store.update(case_id, status="RESOLVED")
    assert case.status == "RESOLVED"
    assert case.supergroup_code == "SG_NEW_ORDER"


def test_reclassify_into_needs_triage_still_blocks_resolve(store: CaseStore):
    case_id = _open(store, supergroup_code="SG_NEW_ORDER")
    store.update(case_id, supergroup_code="SG_NEEDS_TRIAGE")
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
    the resulting state is non-NEEDS_TRIAGE + RESOLVED."""
    case_id = _open(store, supergroup_code="SG_NEEDS_TRIAGE")
    case = store.update(
        case_id,
        supergroup_code="SG_NEW_ORDER",
        status="RESOLVED",
    )
    assert case.supergroup_code == "SG_NEW_ORDER"
    assert case.status == "RESOLVED"
