"""End-to-end — one PO that trips several exceptions (multi-issue case).

A real CPG purchase order routinely produces more than one
exception: a price mismatch, a duplicate retransmission, a
back-order, etc. Each inbound event runs its own
Skill -> Shadow -> Recipe pipeline and persists its own
`ChildCase`; the `OrderCase` is the aggregator that
correlates them by customer PO.

This module drives that path through the real in-process API
(`TestClient`, no network) and pins three contracts:

  * **Correlation dedup** — N events carrying the same
    `metadata.customer_po_number` resolve to exactly ONE case
    (`case_resolver.resolve_or_open_case` -> `lookup_or_create`).
  * **Attached-record loader** — `GET /cases/{id}/records` returns
    all N child records.
  * **Status aggregation (ADR-038 §6.1)** — `GET /cases/{id}`
    reports the status of the least-settled child
    (OPEN_AWAITING_HUMAN > BLOCKED > FAILED > RESOLVED). This is
    the backend counterpart of the asoe-ui `aggregateCaseStatus`
    mock derivation.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.pubsub import event_publisher
from api.store import case_store, exception_store


@pytest.fixture(autouse=True)
def _reset():
    case_store.clear()
    exception_store.clear()
    if hasattr(event_publisher, "clear"):
        event_publisher.clear()
    yield
    case_store.clear()
    exception_store.clear()


@pytest.fixture()
def client():
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def analyst_token():
    return create_test_token(roles=["analyst"], org="tenant-a")


@pytest.fixture()
def manager_token():
    return create_test_token(roles=["manager"], org="tenant-a")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# The shared customer PO every event in the cluster references. The
# per-event `order_id` differs (each exception has its own event id)
# but the PO correlation key is what binds them to one case.
_SHARED_PO = "PO-MULTI-ISSUE-0001"


def _dup_po_event(order_id: str, *, high_confidence: bool) -> dict:
    """A DUPLICATE_PO event for the shared PO.

    High-confidence signal scores auto-block (final_status=BLOCKED);
    medium-confidence scores route to MANUAL_REVIEW_REQUIRED. Both
    shapes are the ones pinned deterministic by
    `test_e2e_duplicate_po.py`.
    """
    if high_confidence:
        signal_scores = {
            "po_number": 1.0, "customer_id": 1.0, "line_items": 0.95,
            "amount": 0.90, "timestamp": 0.80, "ship_to": 0.80,
            "channel": 1.0, "delivery_date": 0.80,
        }
    else:
        signal_scores = {
            "po_number": 0.80, "customer_id": 1.0, "line_items": 0.70,
            "amount": 0.75, "timestamp": 0.30, "ship_to": 0.60,
            "channel": 1.0, "delivery_date": 0.50,
        }
    return {
        "order_id": order_id,
        "line_item": 1,
        "po_price": 100.0,
        "sap_base_price": 100.0,
        "event_type": "EDI_850_DUPLICATE_PO",
        "retailer_id": "R-10",
        "line_count": 1,
        "metadata": {
            # The correlation key — shared across the cluster so all
            # three events materialise onto one case.
            "customer_po_number": _SHARED_PO,
            "signal_scores": signal_scores,
            "matched_po_id": "PO-DUP-ORIGINAL",
        },
    }


def _resolve(client, token, event: dict) -> dict:
    r = client.post(
        "/api/v1/exceptions/resolve", json=event, headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    return r.json()


class TestMultiIssueCase:
    def test_three_events_one_po_materialise_one_case(
        self, client, analyst_token,
    ):
        """Correlation dedup — three events, one shared PO, one case."""
        for i in range(3):
            _resolve(
                client, analyst_token,
                _dup_po_event(f"SO-MI-{i}", high_confidence=True),
            )
        # Exactly one case for the shared PO (lookup_or_create dedup).
        r = client.get(
            f"/api/v1/cases?q={_SHARED_PO}", headers=_auth(analyst_token),
        )
        assert r.status_code == 200, r.text
        cases = r.json()["items"]
        assert len(cases) == 1, f"expected one case, got {len(cases)}"
        assert cases[0]["customer_po_number"] == _SHARED_PO

    def test_attached_records_loader_returns_every_child(
        self, client, analyst_token,
    ):
        """GET /cases/{id}/records returns all N siblings."""
        exc_ids = [
            _resolve(
                client, analyst_token,
                _dup_po_event(f"SO-MI-{i}", high_confidence=True),
            )["exception_id"]
            for i in range(3)
        ]
        # Resolve the parent case via the first exception's detail.
        detail = client.get(
            f"/api/v1/exceptions/{exc_ids[0]}", headers=_auth(analyst_token),
        ).json()
        case_id = detail["parent_case_id"]
        assert case_id, "exception must carry a parent_case_id"

        records = client.get(
            f"/api/v1/cases/{case_id}/records", headers=_auth(analyst_token),
        ).json()
        assert records["total"] == 3
        assert {item["id"] for item in records["items"]} == set(exc_ids)
        # Every sibling points back at the same parent case.
        for item in records["items"]:
            assert item["parent_case_id"] == case_id

    def test_case_status_aggregates_to_least_settled_child(
        self, client, analyst_token,
    ):
        """ADR-038 §6.1 — a YELLOW sibling holds the whole case at
        OPEN_AWAITING_HUMAN even though the others auto-blocked."""
        # Two high-confidence events auto-block; one medium-confidence
        # event needs a human.
        blocked_a = _resolve(
            client, analyst_token,
            _dup_po_event("SO-MI-A", high_confidence=True),
        )
        review_b = _resolve(
            client, analyst_token,
            _dup_po_event("SO-MI-B", high_confidence=False),
        )
        blocked_c = _resolve(
            client, analyst_token,
            _dup_po_event("SO-MI-C", high_confidence=True),
        )
        # The pipeline outcomes this aggregation rests on.
        assert blocked_a["final_status"] == "BLOCKED"
        assert review_b["final_status"] == "MANUAL_REVIEW_REQUIRED"
        assert blocked_c["final_status"] == "BLOCKED"

        detail = client.get(
            f"/api/v1/exceptions/{blocked_a['exception_id']}",
            headers=_auth(analyst_token),
        ).json()
        case_id = detail["parent_case_id"]

        case = client.get(
            f"/api/v1/cases/{case_id}", headers=_auth(analyst_token),
        ).json()
        # MANUAL_REVIEW_REQUIRED -> OPEN_AWAITING_HUMAN dominates the
        # two BLOCKED siblings: the case is not done while a human
        # still owes a decision on one record.
        assert case["status"] == "OPEN_AWAITING_HUMAN"
        # Non-terminal aggregate — the case is not closed.
        assert case["closed_at"] is None

    def test_disposition_re_aggregates_parent_case(
        self, client, analyst_token, manager_token,
    ):
        """A disposition that resolves one child re-aggregates the
        parent case — and once every child is resolved the case
        rolls up to RESOLVED (ADR-038 §6.1 / tasks.md 29.7)."""
        po = "PO-MI-DISPOSITION"
        # Two YELLOW price-mismatch events for one PO, resolved in
        # explain mode → both route to MANUAL_REVIEW_REQUIRED, so the
        # case sits at OPEN_AWAITING_HUMAN.
        exc_ids = []
        for i in range(2):
            r = client.post(
                "/api/v1/exceptions/resolve/explain",
                json={
                    "order_id": f"SO-MI-DISP-{i}",
                    "po_price": 100.0,
                    "sap_base_price": 120.0,
                    "event_type": "EDI_850_PRICE_MISMATCH",
                    "metadata": {"customer_po_number": po},
                },
                headers=_auth(analyst_token),
            )
            assert r.status_code == 200, r.text
            assert r.json()["final_status"] == "MANUAL_REVIEW_REQUIRED"
            exc_ids.append(r.json()["exception_id"])

        detail = client.get(
            f"/api/v1/exceptions/{exc_ids[0]}", headers=_auth(analyst_token),
        ).json()
        case_id = detail["parent_case_id"]
        assert case_id

        def _case_status() -> dict:
            return client.get(
                f"/api/v1/cases/{case_id}", headers=_auth(analyst_token),
            ).json()

        assert _case_status()["status"] == "OPEN_AWAITING_HUMAN"

        # Disposition the first record → RESOLVED. One sibling is still
        # awaiting review, so the case stays at OPEN_AWAITING_HUMAN.
        d1 = client.patch(
            f"/api/v1/exceptions/{exc_ids[0]}/disposition",
            json={
                "action": "ALLOW_BOTH",
                "notes": "Reviewed — price variance approved.",
                "reason_tag": "OTHER",
            },
            headers=_auth(manager_token),
        )
        assert d1.status_code == 200, d1.text
        assert d1.json()["lifecycle_state"] == "RESOLVED"
        assert _case_status()["status"] == "OPEN_AWAITING_HUMAN"

        # Disposition the second record → every child is now resolved,
        # so the case rolls up to RESOLVED and stamps closed_at.
        d2 = client.patch(
            f"/api/v1/exceptions/{exc_ids[1]}/disposition",
            json={
                "action": "ALLOW_BOTH",
                "notes": "Reviewed — price variance approved.",
                "reason_tag": "OTHER",
            },
            headers=_auth(manager_token),
        )
        assert d2.status_code == 200, d2.text
        final = _case_status()
        assert final["status"] == "RESOLVED"
        assert final["closed_at"] is not None

    def test_rejected_child_closes_the_case(
        self, client, analyst_token, manager_token,
    ):
        """A NO_ACTION disposition (REJECT) is a completed human
        decision — the record goes REJECTED and the parent case rolls
        up to RESOLVED, not stuck OPEN_AWAITING_HUMAN."""
        r = client.post(
            "/api/v1/exceptions/resolve/explain",
            json={
                "order_id": "SO-REJECT-1",
                "po_price": 100.0,
                "sap_base_price": 120.0,
                "event_type": "EDI_850_PRICE_MISMATCH",
                "metadata": {"customer_po_number": "PO-REJECT-CASE"},
            },
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["final_status"] == "MANUAL_REVIEW_REQUIRED"
        exc_id = r.json()["exception_id"]

        detail = client.get(
            f"/api/v1/exceptions/{exc_id}", headers=_auth(analyst_token),
        ).json()
        case_id = detail["parent_case_id"]
        before = client.get(
            f"/api/v1/cases/{case_id}", headers=_auth(analyst_token),
        ).json()
        assert before["status"] == "OPEN_AWAITING_HUMAN"

        # Reject the exception — action NO_ACTION → sub_type REJECT.
        d = client.patch(
            f"/api/v1/exceptions/{exc_id}/disposition",
            json={
                "action": "NO_ACTION",
                "notes": "Reviewed — price variance within tolerance, no action.",
                "reason_tag": "OTHER",
            },
            headers=_auth(manager_token),
        )
        assert d.status_code == 200, d.text
        assert d.json()["lifecycle_state"] == "REJECTED"

        after = client.get(
            f"/api/v1/cases/{case_id}", headers=_auth(analyst_token),
        ).json()
        # The rejected child is settled — the case closes.
        assert after["status"] == "RESOLVED"
        assert after["closed_at"] is not None

    def test_escalate_reopens_a_resolved_case(self, client, analyst_token):
        """A second HITL endpoint (`/escalate`) re-aggregates too:
        escalating the only child of a BLOCKED case reopens it to
        OPEN_AWAITING_HUMAN and clears the stale closed_at."""
        exc = _resolve(
            client, analyst_token,
            _dup_po_event("SO-ESC-1", high_confidence=True),
        )
        assert exc["final_status"] == "BLOCKED"
        detail = client.get(
            f"/api/v1/exceptions/{exc['exception_id']}",
            headers=_auth(analyst_token),
        ).json()
        case_id = detail["parent_case_id"]

        before = client.get(
            f"/api/v1/cases/{case_id}", headers=_auth(analyst_token),
        ).json()
        assert before["status"] == "BLOCKED"
        assert before["closed_at"] is not None

        # Escalate the blocked child — it moves to ESCALATED.
        esc = client.post(
            f"/api/v1/exceptions/{exc['exception_id']}/escalate",
            json={"reason": "Manual review of the duplicate block.",
                  "to_role": "manager"},
            headers=_auth(analyst_token),
        )
        assert esc.status_code == 200, esc.text
        assert esc.json()["lifecycle_state"] == "ESCALATED"

        after = client.get(
            f"/api/v1/cases/{case_id}", headers=_auth(analyst_token),
        ).json()
        # The case reopened — no longer terminal, closed_at dropped.
        assert after["status"] == "OPEN_AWAITING_HUMAN"
        assert after["closed_at"] is None

    def test_all_blocked_children_aggregate_to_blocked(
        self, client, analyst_token,
    ):
        """When every child auto-blocks, the case sits at BLOCKED."""
        exc = _resolve(
            client, analyst_token,
            _dup_po_event("SO-ALLBLK-A", high_confidence=True),
        )
        _resolve(
            client, analyst_token,
            _dup_po_event("SO-ALLBLK-B", high_confidence=True),
        )
        detail = client.get(
            f"/api/v1/exceptions/{exc['exception_id']}",
            headers=_auth(analyst_token),
        ).json()
        case = client.get(
            f"/api/v1/cases/{detail['parent_case_id']}",
            headers=_auth(analyst_token),
        ).json()
        assert case["status"] == "BLOCKED"
        # BLOCKED is terminal (ADR-038 §6.1) — closed_at is stamped.
        assert case["closed_at"] is not None
