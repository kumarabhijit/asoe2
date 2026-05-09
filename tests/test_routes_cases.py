"""ADR-038 Phase H.6 — `/api/v1/cases/*` route tests.

Covers:
  * GET /api/v1/cases — empty, list, filter by source/status, sort,
    limit cap, tenant isolation.
  * GET /api/v1/cases/{id} — found, missing, cross-tenant 404.
  * RBAC — analyst/manager/admin/viewer/partner can read; partner-
    role scoping derives from child exception records.
  * Assigned-account scoping — derived from child records or the
    case's customer_id.

The store is in-memory; tests reset both `case_store` and
`exception_store` per test.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.store import ExceptionRecord, case_store, exception_store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    case_store.clear()
    exception_store.clear()
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def analyst_token():
    return create_test_token(roles=["analyst"], org="tenant-a")


@pytest.fixture()
def manager_token():
    return create_test_token(roles=["manager"], org="tenant-a")


@pytest.fixture()
def viewer_token():
    return create_test_token(roles=["viewer"], org="tenant-a")


@pytest.fixture()
def partner_token():
    return create_test_token(
        roles=["partner"], org="tenant-a", retailer_id="WALMART",
    )


@pytest.fixture()
def assigned_token():
    return create_test_token(
        roles=["analyst"], org="tenant-a",
        assigned_accounts=["acc-001"],
    )


@pytest.fixture()
def tenant_b_token():
    return create_test_token(roles=["analyst"], org="tenant-b")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_case(
    tenant_id: str = "tenant-a",
    *,
    source: str = "automated_order",
    source_channel: str = "edi_x12_850",
    customer_po_number: str | None = None,
    customer_id: str | None = None,
):
    case, _ = case_store.lookup_or_create(
        tenant_id,
        source=source,
        source_channel=source_channel,
        customer_po_number=customer_po_number,
        customer_id=customer_id,
    )
    return case


def _attach_child(
    *,
    tenant_id: str = "tenant-a",
    order_id: str,
    parent_case_id: str,
    account_id: str | None = None,
) -> ExceptionRecord:
    """Inject a child ExceptionRecord with the fields the in-memory
    `create()` shorthand omits (`account_id`). Mirrors what the live
    persistence path produces in orchestration."""
    record = ExceptionRecord(
        tenant_id=tenant_id,
        order_id=order_id,
        event_type="EDI_850_PRICE_MISMATCH",
        trace_id=f"trace-{order_id}",
        parent_case_id=parent_case_id,
        account_id=account_id,
    )
    exception_store._records[record.id] = record
    return record


# ---------------------------------------------------------------------------
# Auth + tenant isolation
# ---------------------------------------------------------------------------

class TestAuth:
    def test_no_auth_rejected(self, client):
        r = client.get("/api/v1/cases")
        assert r.status_code in (401, 403)

    def test_analyst_can_list(self, client, analyst_token):
        r = client.get("/api/v1/cases", headers=_auth(analyst_token))
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert body["total"] == 0

    def test_manager_can_list(self, client, manager_token):
        r = client.get("/api/v1/cases", headers=_auth(manager_token))
        assert r.status_code == 200

    def test_viewer_can_list(self, client, viewer_token):
        r = client.get("/api/v1/cases", headers=_auth(viewer_token))
        assert r.status_code == 200

    def test_tenant_isolation(self, client, tenant_b_token):
        _open_case(tenant_id="tenant-a", customer_po_number="PO-A1")
        r = client.get("/api/v1/cases", headers=_auth(tenant_b_token))
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert body["total"] == 0


# ---------------------------------------------------------------------------
# List endpoint
# ---------------------------------------------------------------------------

class TestList:
    def test_list_returns_open_cases(self, client, analyst_token):
        c1 = _open_case(customer_po_number="PO-1")
        c2 = _open_case(customer_po_number="PO-2", source="manual_order",
                        source_channel="email")
        r = client.get("/api/v1/cases", headers=_auth(analyst_token))
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        ids = {item["case_id"] for item in body["items"]}
        assert ids == {c1.case_id, c2.case_id}

    def test_filter_by_source(self, client, analyst_token):
        _open_case(customer_po_number="PO-1", source="automated_order")
        manual = _open_case(
            customer_po_number="PO-2", source="manual_order",
            source_channel="email",
        )
        r = client.get(
            "/api/v1/cases?source=manual_order",
            headers=_auth(analyst_token),
        )
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["case_id"] == manual.case_id

    def test_filter_by_status(self, client, analyst_token):
        c1 = _open_case(customer_po_number="PO-1")
        c2 = _open_case(customer_po_number="PO-2")
        case_store.update(c2.case_id, status="RESOLVED")
        r = client.get(
            "/api/v1/cases?status=RESOLVED",
            headers=_auth(analyst_token),
        )
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["case_id"] == c2.case_id

    def test_sorted_newest_first(self, client, analyst_token):
        _open_case(customer_po_number="PO-1")
        c2 = _open_case(customer_po_number="PO-2")
        # Force c2 to look newer than c1.
        case_store.update(c2.case_id, opened_at="9999-01-01T00:00:00+00:00")
        r = client.get("/api/v1/cases", headers=_auth(analyst_token))
        items = r.json()["items"]
        assert items[0]["case_id"] == c2.case_id

    def test_limit_caps_results_but_total_unbounded(
        self, client, analyst_token,
    ):
        for i in range(5):
            _open_case(customer_po_number=f"PO-{i}")
        r = client.get(
            "/api/v1/cases?limit=2", headers=_auth(analyst_token),
        )
        body = r.json()
        assert len(body["items"]) == 2
        assert body["total"] == 5


# ---------------------------------------------------------------------------
# Detail endpoint
# ---------------------------------------------------------------------------

class TestDetail:
    def test_get_existing(self, client, analyst_token):
        case = _open_case(customer_po_number="PO-X")
        r = client.get(
            f"/api/v1/cases/{case.case_id}",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["case_id"] == case.case_id
        assert body["customer_po_number"] == "PO-X"
        assert body["source"] == "automated_order"

    def test_missing_returns_404(self, client, analyst_token):
        r = client.get(
            "/api/v1/cases/does-not-exist",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 404

    def test_cross_tenant_returns_404(self, client, tenant_b_token):
        case = _open_case(
            tenant_id="tenant-a", customer_po_number="PO-Z",
        )
        r = client.get(
            f"/api/v1/cases/{case.case_id}",
            headers=_auth(tenant_b_token),
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Partner / assigned-account scoping
# ---------------------------------------------------------------------------

class TestScoping:
    def test_partner_sees_only_matching_prefix(
        self, client, partner_token,
    ):
        # Case A has a child whose order_id starts with WALMART → in scope.
        case_a = _open_case(customer_po_number="PO-WMT")
        _attach_child(order_id="WALMART-001", parent_case_id=case_a.case_id)
        # Case B has only a Target child → out of scope.
        case_b = _open_case(customer_po_number="PO-TGT")
        _attach_child(order_id="TARGET-007", parent_case_id=case_b.case_id)
        r = client.get("/api/v1/cases", headers=_auth(partner_token))
        body = r.json()
        ids = {item["case_id"] for item in body["items"]}
        assert ids == {case_a.case_id}

    def test_assigned_accounts_via_child(self, client, assigned_token):
        case_a = _open_case(customer_po_number="PO-A")
        _attach_child(
            order_id="ANY-1", parent_case_id=case_a.case_id,
            account_id="acc-001",
        )
        case_b = _open_case(customer_po_number="PO-B")
        _attach_child(
            order_id="ANY-2", parent_case_id=case_b.case_id,
            account_id="acc-999",
        )
        r = client.get("/api/v1/cases", headers=_auth(assigned_token))
        ids = {item["case_id"] for item in r.json()["items"]}
        assert ids == {case_a.case_id}

    def test_assigned_accounts_via_case_customer_id(
        self, client, assigned_token,
    ):
        # Just-opened Manual Order case with no child yet, but
        # customer_id matches the assigned-accounts allowlist.
        case = _open_case(
            customer_po_number="PO-EARLY",
            customer_id="acc-001",
            source="manual_order",
            source_channel="email",
        )
        r = client.get("/api/v1/cases", headers=_auth(assigned_token))
        ids = {item["case_id"] for item in r.json()["items"]}
        assert case.case_id in ids

    def test_partner_detail_404_when_out_of_scope(
        self, client, partner_token,
    ):
        case = _open_case(customer_po_number="PO-NO-WMT")
        _attach_child(order_id="TARGET-999", parent_case_id=case.case_id)
        r = client.get(
            f"/api/v1/cases/{case.case_id}",
            headers=_auth(partner_token),
        )
        assert r.status_code == 404
