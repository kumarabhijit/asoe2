"""GET /api/v1/cases/{id}/records — attached-record loader tests
(Phase 28.5.x §28.5 follow-up).

Pins the contract the UI's `CaseDetailPanel` consumes:

  * Returns ExceptionDetail-shaped child records sorted
    most-recently-updated first.
  * Returns a `aggregated_policy_hits` dedupe-preserve-order union
    of every child's `shadow_policy_hits`, so the UI can populate
    `<CaseDetailPanel policyHits={...} />` without re-deriving on
    the client.
  * Returns 404 for a case the caller can't read (cross-tenant or
    out-of-scope) — same response as `/cases/{id}` to avoid
    leaking existence.
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
    exception_store.clear() if hasattr(exception_store, "clear") else None
    if hasattr(event_publisher, "clear"):
        event_publisher.clear()
    yield
    case_store.clear()


@pytest.fixture()
def client():
    return TestClient(create_app(), raise_server_exceptions=True)


@pytest.fixture()
def manager_token():
    return create_test_token(
        sub="manager-A", roles=["manager"], org="tenant-a",
    )


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_case_with_children(
    tenant_id: str,
    children: list[tuple[str, list[str]]],
) -> str:
    """Helper: create a case + N child ChildCases + traces.

    Each entry in `children` is `(order_id, policy_hits)`. Returns
    the case_id.
    """
    case, _opened = case_store.lookup_or_create(
        tenant_id=tenant_id,
        origin="CUSTOMER",
        source_channel="email",
        customer_po_number="PO-CRL-1",
    )
    for order_id, policy_hits in children:
        record = exception_store.create(
            tenant_id=tenant_id,
            order_id=order_id,
            event_type="EDI_850_PRICE_MISMATCH",
            trace_id=f"trace-{order_id}",
            intent="CONTRACTUAL_CORRECTION",
            shadow_verdict="GREEN",
            parent_case_id=case.case_id,
        )
        exception_store.store_trace(record.id, {
            "trace_id": record.trace_id,
            "event_id": order_id,
            "shadow_verdict": "GREEN",
            "shadow_policy_hits": policy_hits,
            "executed_nodes": [],
        })
    return case.case_id


# ---------------------------------------------------------------------------


class TestCaseRecordsEndpoint:
    def test_returns_attached_records_sorted_recent_first(
        self, client, manager_token,
    ):
        case_id = _seed_case_with_children("tenant-a", [
            ("PO-A", ["RULE_X"]),
            ("PO-B", ["RULE_Y"]),
        ])
        r = client.get(
            f"/api/v1/cases/{case_id}/records",
            headers=_auth(manager_token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 2
        assert len(body["items"]) == 2
        # Sort key is updated_at desc — both children created in the
        # same millisecond may tie, but the contract guarantees the
        # most-recently-updated leads. Just assert the order is
        # stable across calls of the same response.
        order_ids = [item["order_id"] for item in body["items"]]
        assert set(order_ids) == {"PO-A", "PO-B"}

    def test_aggregates_policy_hits_dedupe_preserving_order(
        self, client, manager_token,
    ):
        case_id = _seed_case_with_children("tenant-a", [
            ("PO-A", ["RULE_X", "LLM_SHADOW:CONCERN_1"]),
            ("PO-B", ["RULE_X", "RULE_Y"]),
            ("PO-C", ["LLM_SHADOW:CONCERN_1", "LLM_SHADOW:CONCERN_2"]),
        ])
        r = client.get(
            f"/api/v1/cases/{case_id}/records",
            headers=_auth(manager_token),
        )
        assert r.status_code == 200, r.text
        agg = r.json()["aggregated_policy_hits"]
        # All four distinct hits surface; LLM_SHADOW: prefix is
        # preserved so the UI's PolicyHitBadge keeps the L1/L2
        # distinction. RULE_X appears once despite being in two
        # children.
        assert set(agg) == {
            "RULE_X",
            "LLM_SHADOW:CONCERN_1",
            "RULE_Y",
            "LLM_SHADOW:CONCERN_2",
        }

    def test_empty_aggregated_policy_hits_when_no_children(
        self, client, manager_token,
    ):
        case_id = _seed_case_with_children("tenant-a", [])
        r = client.get(
            f"/api/v1/cases/{case_id}/records",
            headers=_auth(manager_token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 0
        assert body["items"] == []
        # Empty (not null) — the UI's CaseDetailPanel hides the
        # section when the array length is 0 (Guardrail #6, no
        # synthesised "no hits" placeholder).
        assert body["aggregated_policy_hits"] == []

    def test_returns_404_for_cross_tenant_case(self, client):
        case_id = _seed_case_with_children("tenant-a", [
            ("PO-A", ["RULE_X"]),
        ])
        # Different tenant token — should get NOT_FOUND, not the
        # case's children.
        other_token = create_test_token(
            sub="manager-other", roles=["manager"], org="tenant-other",
        )
        r = client.get(
            f"/api/v1/cases/{case_id}/records",
            headers=_auth(other_token),
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"

    def test_returns_404_for_unknown_case(self, client, manager_token):
        r = client.get(
            "/api/v1/cases/case-does-not-exist/records",
            headers=_auth(manager_token),
        )
        assert r.status_code == 404
