"""ADR-038 §6.1 — case-status aggregation tests.

`OrderCase.status` is a roll-up of the case's child records: the
case sits at the status of its *least-settled* child, by the
dominance order OPEN_AWAITING_HUMAN > BLOCKED > FAILED > RESOLVED.

These tests cover the three units in `api/case_resolver.py`:

  * `_case_status_from_lifecycle` — projects one child record's
    `lifecycle_state` onto a candidate `CaseStatus`.
  * `_aggregate_case_status` — folds every attached record plus the
    incoming (not-yet-persisted) event into one `CaseStatus`.
  * `recompute_case_status` — recomputes + persists, stamps
    `closed_at` on a terminal aggregate, skips cosign-parked cases.

Plus the `materialise_for_event` integration: a sibling record
attaching to a case flips the parent status.

Parity note: the projection mirrors the asoe-ui
`caseFromMockException` / `aggregateCaseStatus` derivation 1:1 so
the mock preview layer and the live backend agree (see
`asoe-ui/tests/architectural/case_pivot_mock_wiring.test.ts`).
"""

from __future__ import annotations

import pytest

from api.case_resolver import (
    _aggregate_case_status,
    _case_status_from_lifecycle,
    materialise_for_event,
    recompute_case_status,
)
from api.store import case_store, exception_store
from contracts.models import CasePendingOverride, OrderEvent


@pytest.fixture(autouse=True)
def _reset_stores():
    case_store.clear()
    exception_store.clear()
    yield
    case_store.clear()
    exception_store.clear()


def _open_case(tenant_id: str = "t1", po: str = "PO-AGG-1"):
    """Open a bare case for the tests that need a case_id to attach to."""
    case, _opened = case_store.lookup_or_create(
        tenant_id=tenant_id,
        source="automated_order",
        source_channel="edi_x12_850",
        customer_po_number=po,
    )
    return case


def _attach_record(tenant_id: str, case_id: str, order_id: str, final_status: str):
    """Persist a child ExceptionRecord with the given terminal status.

    `exception_store.create` derives `lifecycle_state` from
    `final_status` via `STATUS_TO_LIFECYCLE` — exactly the path the
    real `/resolve` persistence uses.
    """
    return exception_store.create(
        tenant_id=tenant_id,
        order_id=order_id,
        event_type="EDI_850_PRICE_MISMATCH",
        trace_id=f"trace-{order_id}",
        final_status=final_status,
        parent_case_id=case_id,
    )


# ---------------------------------------------------------------------------
# _case_status_from_lifecycle — per-record projection
# ---------------------------------------------------------------------------


class TestCaseStatusFromLifecycle:
    @pytest.mark.parametrize("lifecycle", ["RESOLVED", "CLOSED"])
    def test_resolved_and_closed_project_to_resolved(self, lifecycle):
        assert _case_status_from_lifecycle(lifecycle) == "RESOLVED"

    def test_blocked_projects_to_blocked(self):
        assert _case_status_from_lifecycle("BLOCKED") == "BLOCKED"

    def test_failed_projects_to_failed(self):
        assert _case_status_from_lifecycle("FAILED") == "FAILED"

    @pytest.mark.parametrize(
        "lifecycle",
        ["PENDING_REVIEW", "ESCALATED", "PENDING_ADMIN_REVIEW",
         "PENDING_COSIGN", "REJECTED", "INGESTED", "CLASSIFYING", "AUDITING"],
    )
    def test_unsettled_lifecycles_project_to_awaiting_human(self, lifecycle):
        # Every non-terminal (and the human-rejected) lifecycle means
        # someone still owes forward progress — mirrors the asoe-ui
        # `caseFromMockException` default branch.
        assert _case_status_from_lifecycle(lifecycle) == "OPEN_AWAITING_HUMAN"


# ---------------------------------------------------------------------------
# _aggregate_case_status — dominance roll-up
# ---------------------------------------------------------------------------


class TestAggregateCaseStatus:
    def test_single_resolved_incoming_only(self):
        case = _open_case()
        # No persisted children — only the incoming COMPLETE event.
        assert _aggregate_case_status("t1", case.case_id, "COMPLETE") == "RESOLVED"

    def test_all_children_resolved_is_resolved(self):
        case = _open_case()
        _attach_record("t1", case.case_id, "SO-1", "COMPLETE")
        _attach_record("t1", case.case_id, "SO-2", "COMPLETE")
        assert _aggregate_case_status("t1", case.case_id, "COMPLETE") == "RESOLVED"

    def test_one_pending_child_dominates_resolved_siblings(self):
        case = _open_case()
        _attach_record("t1", case.case_id, "SO-1", "COMPLETE")
        _attach_record("t1", case.case_id, "SO-2", "COMPLETE")
        # Incoming record is still awaiting a human.
        assert (
            _aggregate_case_status("t1", case.case_id, "MANUAL_REVIEW_REQUIRED")
            == "OPEN_AWAITING_HUMAN"
        )

    def test_pending_dominates_blocked(self):
        case = _open_case()
        _attach_record("t1", case.case_id, "SO-1", "BLOCKED")
        assert (
            _aggregate_case_status("t1", case.case_id, "MANUAL_REVIEW_REQUIRED")
            == "OPEN_AWAITING_HUMAN"
        )

    def test_blocked_dominates_failed_and_resolved(self):
        case = _open_case()
        _attach_record("t1", case.case_id, "SO-1", "FAIL_TO_HUMAN")
        _attach_record("t1", case.case_id, "SO-2", "COMPLETE")
        assert _aggregate_case_status("t1", case.case_id, "BLOCKED") == "BLOCKED"

    def test_failed_dominates_resolved(self):
        case = _open_case()
        _attach_record("t1", case.case_id, "SO-1", "COMPLETE")
        assert (
            _aggregate_case_status("t1", case.case_id, "FAIL_TO_HUMAN") == "FAILED"
        )

    def test_audit_context_missing_projects_to_failed(self):
        # AUDIT_CONTEXT_MISSING → lifecycle FAILED (no reviewer path).
        case = _open_case()
        assert (
            _aggregate_case_status("t1", case.case_id, "AUDIT_CONTEXT_MISSING")
            == "FAILED"
        )

    def test_rejected_incoming_projects_to_awaiting_human(self):
        # REJECTED → lifecycle REJECTED → OPEN_AWAITING_HUMAN, parity
        # with the asoe-ui `caseFromMockException` default branch.
        case = _open_case()
        assert (
            _aggregate_case_status("t1", case.case_id, "REJECTED")
            == "OPEN_AWAITING_HUMAN"
        )


# ---------------------------------------------------------------------------
# recompute_case_status — persist + closed_at + cosign skip
# ---------------------------------------------------------------------------


class TestRecomputeCaseStatus:
    def test_unknown_case_returns_none_unchanged(self):
        case, changed = recompute_case_status("t1", "no-such-case", "COMPLETE")
        assert case is None
        assert changed is False

    def test_status_change_persists_and_reports_changed(self):
        case = _open_case()
        assert case.status == "OPEN_AGENT_PROCESSING"  # OrderCase default
        updated, changed = recompute_case_status(
            "t1", case.case_id, "MANUAL_REVIEW_REQUIRED",
        )
        assert changed is True
        assert updated.status == "OPEN_AWAITING_HUMAN"
        # Persisted — a fresh get sees the new status.
        assert case_store.get(case.case_id).status == "OPEN_AWAITING_HUMAN"

    def test_no_change_reports_unchanged(self):
        case = _open_case()
        recompute_case_status("t1", case.case_id, "MANUAL_REVIEW_REQUIRED")
        # Recompute again with the same aggregate — no-op.
        _attach_record("t1", case.case_id, "SO-1", "MANUAL_REVIEW_REQUIRED")
        updated, changed = recompute_case_status(
            "t1", case.case_id, "MANUAL_REVIEW_REQUIRED",
        )
        assert changed is False
        assert updated.status == "OPEN_AWAITING_HUMAN"

    def test_terminal_aggregate_stamps_closed_at(self):
        case = _open_case()
        assert case.closed_at is None
        updated, changed = recompute_case_status("t1", case.case_id, "COMPLETE")
        assert changed is True
        assert updated.status == "RESOLVED"
        assert updated.closed_at is not None

    def test_non_terminal_aggregate_leaves_closed_at_none(self):
        case = _open_case()
        updated, _changed = recompute_case_status(
            "t1", case.case_id, "MANUAL_REVIEW_REQUIRED",
        )
        assert updated.status == "OPEN_AWAITING_HUMAN"
        assert updated.closed_at is None

    def test_reopen_clears_stale_closed_at(self):
        # A case that auto-blocked on its first record (terminal,
        # closed_at stamped) then takes a sibling needing review must
        # roll back to OPEN_AWAITING_HUMAN AND drop the close stamp.
        case = _open_case()
        blocked, _ = recompute_case_status("t1", case.case_id, "BLOCKED")
        assert blocked.status == "BLOCKED"
        assert blocked.closed_at is not None
        # The blocked record persists, then a review sibling arrives.
        _attach_record("t1", case.case_id, "SO-1", "BLOCKED")
        reopened, changed = recompute_case_status(
            "t1", case.case_id, "MANUAL_REVIEW_REQUIRED",
        )
        assert changed is True
        assert reopened.status == "OPEN_AWAITING_HUMAN"
        assert reopened.closed_at is None

    def test_cosign_parked_case_is_skipped(self):
        # A case with a staged pending_override is owned by the cosign
        # flow — recompute must not clobber its status.
        case = _open_case()
        parked = case_store.set_pending_override(
            case.case_id,
            CasePendingOverride(
                initiator="manager-A",
                initiated_at="2026-05-15T00:00:00Z",
                pending_action="APPROVE",
            ),
        )
        assert parked.status == "OPEN_AWAITING_HUMAN"  # set by set_pending_override
        result, changed = recompute_case_status("t1", case.case_id, "COMPLETE")
        assert changed is False
        # Status untouched despite the COMPLETE incoming record.
        assert result.status == "OPEN_AWAITING_HUMAN"
        assert case_store.get(case.case_id).pending_override is not None


# ---------------------------------------------------------------------------
# materialise_for_event — sibling attach flips the parent status
# ---------------------------------------------------------------------------


class TestMaterialiseAggregation:
    def _event(self, order_id: str, po: str) -> OrderEvent:
        return OrderEvent(
            order_id=order_id,
            po_price=100.0,
            sap_base_price=100.0,
            event_type="EDI_850_PRICE_MISMATCH",
            retailer_id="R-1",
            metadata={"customer_po_number": po},
        )

    def test_first_clean_event_opens_resolved_case(self):
        case = materialise_for_event("t1", self._event("SO-1", "PO-1"), "COMPLETE")
        assert case is not None
        # A clean first event opens a case that is already resolved.
        assert case.status == "RESOLVED"
        assert case.closed_at is not None

    def test_sibling_attach_flips_resolved_case_to_awaiting_human(self):
        po = "PO-MULTI"
        # First event resolves clean → case RESOLVED.
        case1 = materialise_for_event("t1", self._event("SO-A", po), "COMPLETE")
        # Persist the first record so the second materialise sees it
        # (the real /resolve path persists immediately after materialise).
        _attach_record("t1", case1.case_id, "SO-A", "COMPLETE")
        assert case1.status == "RESOLVED"

        # Second event for the SAME PO needs a human → the case is no
        # longer "done"; it rolls back to OPEN_AWAITING_HUMAN.
        case2 = materialise_for_event(
            "t1", self._event("SO-B", po), "MANUAL_REVIEW_REQUIRED",
        )
        assert case2.case_id == case1.case_id
        assert case2.status == "OPEN_AWAITING_HUMAN"
        # Only one case across the two correlated events.
        assert len(case_store.list_by_tenant("t1")) == 1
