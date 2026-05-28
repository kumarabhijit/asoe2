"""ADR-041 P3e §3.2 — `case_update` dispatch on child mutations.

Locks the invariant the asoe-ui WebSocket invalidation depends on:
when a child-record mutation could shift the CaseSummary projection
(audit_verdict_color, intent, dollar_impact) WITHOUT flipping
OrderCase.status, a `case_update` event must still fire so the UI
refetches and picks up the new summary.

Three classes of test:

  1. Source-grep architectural lock: the two known entry points
     (`api/case_resolver.py::materialise_for_event` and
     `api/routes/exceptions.py::_reaggregate_parent_case`) must
     call `publish_case_summary_changed` on the no-status-change
     branch.

  2. Behavioural lock: a HITL action that changes
     `financial_impact_usd` without flipping case status must
     emit a `case_update` event with `updated_fields=["case_summary"]`.

  3. The helper itself: `publish_case_summary_changed` must wire
     to the right WSEvent factory + invalidate the intents cache.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from api.case_events import publish_case_summary_changed
from api.case_resolver import materialise_for_event
from api.pubsub import event_publisher
from api.store import case_store, exception_store
from contracts.models import OrderCase, OrderEvent


# ---------------------------------------------------------------------------
# Pattern A — source-grep lock
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[1]


def test_case_resolver_emits_summary_changed_on_no_status_change():
    """`materialise_for_event` must invoke `publish_case_summary_changed`
    on the no-status-change branch — otherwise a child attach that
    flips intent without flipping status leaves the UI stale.
    """
    src = (REPO / "api" / "case_resolver.py").read_text()
    assert "publish_case_summary_changed" in src, (
        "api/case_resolver.py must import + invoke "
        "publish_case_summary_changed (ADR-041 P3e §3.2)."
    )
    # Pattern: `elif not status_changed:` followed by the call.
    assert re.search(
        r"elif\s+not\s+status_changed:[\s\S]{0,400}?publish_case_summary_changed",
        src,
    ), (
        "case_resolver's no-status-change branch must call "
        "publish_case_summary_changed so the UI refetches the new "
        "CaseSummary projection."
    )


def test_routes_exceptions_emits_summary_changed_on_hitl_actions():
    """`_reaggregate_parent_case` is the canonical hook after every
    HITL action (disposition / cosign / escalate / reanalyze /
    challenge / admin-release). It must call
    `publish_case_summary_changed` when status doesn't flip — the
    HITL action may have changed verdict color or dollar impact
    via the recipe rerun."""
    src = (REPO / "api" / "routes" / "exceptions.py").read_text()
    assert "publish_case_summary_changed" in src, (
        "api/routes/exceptions.py must import + invoke "
        "publish_case_summary_changed (ADR-041 P3e §3.2)."
    )
    assert re.search(
        r"_reaggregate_parent_case[\s\S]{0,800}?publish_case_summary_changed",
        src,
    ), (
        "_reaggregate_parent_case must dispatch case_update on the "
        "no-status-change branch so HITL actions that only shift "
        "summary fields still refresh the queue."
    )


# ---------------------------------------------------------------------------
# Helper-level test: publish_case_summary_changed wire shape
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh():
    case_store.clear()
    exception_store.clear()
    if hasattr(event_publisher, "clear"):
        event_publisher.clear()
    yield
    case_store.clear()
    exception_store.clear()
    if hasattr(event_publisher, "clear"):
        event_publisher.clear()


def _open_case() -> OrderCase:
    case, _ = case_store.lookup_or_create(
        "tenant-a",
        origin="API",
        source_channel="edi_x12_850",
        customer_po_number="PO-X",
        customer_id="acme-corp",
    )
    return case


def _recent(tenant_id: str) -> list[dict]:
    """Pull every event emitted to the tenant's channel during the
    test (in-memory pubsub records them as JSON strings)."""
    return [json.loads(s) for s in event_publisher.get_recent(tenant_id)]


def test_publish_case_summary_changed_emits_case_update_with_marker(fresh):
    case = _open_case()
    publish_case_summary_changed(case)
    events = _recent("tenant-a")
    case_updates = [e for e in events if e["type"] == "case_update"]
    assert len(case_updates) == 1
    event = case_updates[0]
    # `case_id` lives at the top level of the WS envelope, not in
    # payload — payload carries the change details.
    assert event["case_id"] == case.case_id
    assert event["payload"]["updated_fields"] == ["case_summary"]


def test_publish_case_summary_changed_invalidates_intents_cache():
    """Source-level lock: the helper MUST invalidate the per-case
    intents cache so the next /cases read re-derives. Without this
    a child intent flip leaves the chip-filter set stale."""
    src = (REPO / "api" / "case_events.py").read_text()
    body = re.search(
        r"def\s+publish_case_summary_changed[\s\S]+?def\s+publish_case_close",
        src,
    )
    assert body is not None, "publish_case_summary_changed not found"
    assert "case_intents_cache.invalidate" in body.group(0), (
        "publish_case_summary_changed must call "
        "case_intents_cache.invalidate — otherwise the cache stays "
        "stale on intent flips."
    )


# ---------------------------------------------------------------------------
# Behavioural lock: end-to-end via materialise_for_event
# ---------------------------------------------------------------------------


def test_attach_event_with_no_status_flip_emits_case_summary_update(fresh):
    """Two events on the same PO produce the same case. The second
    attach goes through `materialise_for_event` -> the case is not
    freshly opened, status doesn't change -> the new case_summary
    update emit MUST fire so the UI refetches."""
    event_a = OrderEvent(
        order_id="PO-1",
        event_type="EMAIL_ORDER_ENTRY_REQUEST",
        po_price=100.0,
        sap_base_price=120.0,
    )
    event_b = OrderEvent(
        order_id="PO-1",
        event_type="EMAIL_ORDER_ENTRY_REQUEST",
        po_price=110.0,
        sap_base_price=120.0,
    )
    case_a = materialise_for_event("tenant-a", event_a, final_status=None)
    assert case_a is not None
    # Snapshot events emitted by the first call; the second call's
    # output is what we're checking.
    events_after_first = len(_recent("tenant-a"))
    case_b = materialise_for_event("tenant-a", event_b, final_status=None)
    assert case_b is not None and case_b.case_id == case_a.case_id

    new_events = _recent("tenant-a")[events_after_first:]
    summary_updates = [
        e for e in new_events
        if e["type"] == "case_update"
        and e["payload"].get("updated_fields") == ["case_summary"]
    ]
    assert len(summary_updates) == 1, (
        f"Expected exactly one case_summary update on the second "
        f"materialise, got {len(summary_updates)}. Events: {new_events}"
    )
