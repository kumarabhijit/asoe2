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
from api.store import ChildCase, case_store, exception_store


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
    origin: str = "API",
    source_channel: str = "edi_x12_850",
    customer_po_number: str | None = None,
    customer_id: str | None = None,
):
    case, _ = case_store.lookup_or_create(
        tenant_id,
        origin=origin,
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
) -> ChildCase:
    """Inject a child ChildCase with the fields the in-memory
    `create()` shorthand omits (`account_id`). Mirrors what the live
    persistence path produces in orchestration."""
    record = ChildCase(
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
        c2 = _open_case(customer_po_number="PO-2", origin="CUSTOMER",
                        source_channel="email")
        r = client.get("/api/v1/cases", headers=_auth(analyst_token))
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        ids = {item["case_id"] for item in body["items"]}
        assert ids == {c1.case_id, c2.case_id}

    def test_filter_by_source(self, client, analyst_token):
        _open_case(customer_po_number="PO-1", origin="API")
        manual = _open_case(
            customer_po_number="PO-2", origin="CUSTOMER",
            source_channel="email",
        )
        r = client.get(
            "/api/v1/cases?origin=CUSTOMER",
            headers=_auth(analyst_token),
        )
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["case_id"] == manual.case_id

    def test_filter_by_supergroup_code(self, client, analyst_token):
        """Requirements §6 — supergroup_code is the case-level intent."""
        email_case = _open_case(
            customer_po_number="PO-1", origin="CUSTOMER",
            source_channel="email",
        )
        case_store.update(
            email_case.case_id, supergroup_code="SG_NEW_ORDER",
            classified_by="user:lead-1", classifier_type="HUMAN",
        )
        _open_case(customer_po_number="PO-2", origin="API")
        r = client.get(
            "/api/v1/cases?supergroup_code=SG_NEW_ORDER",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["case_id"] == email_case.case_id
        assert body["items"][0]["supergroup_code"] == "SG_NEW_ORDER"

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
# Cursor pagination (ADR-038 §D7 amendment, 2026-05-11)
# ---------------------------------------------------------------------------

class TestCursorPagination:
    """Cursor pagination on /api/v1/cases — added after the case-projected
    exception queue surfaced as silently capped at the first page."""

    def test_first_page_returns_cursor_when_more_exist(
        self, client, analyst_token,
    ):
        for i in range(5):
            _open_case(customer_po_number=f"PO-{i:03d}")
        r = client.get(
            "/api/v1/cases?limit=2", headers=_auth(analyst_token),
        )
        body = r.json()
        assert body["has_more"] is True
        assert body["cursor"] is not None
        # Cursor is the case_id of the last item on the page.
        assert body["cursor"] == body["items"][-1]["case_id"]

    def test_last_page_omits_cursor(self, client, analyst_token):
        for i in range(3):
            _open_case(customer_po_number=f"PO-{i:03d}")
        r = client.get(
            "/api/v1/cases?limit=10", headers=_auth(analyst_token),
        )
        body = r.json()
        assert body["has_more"] is False
        assert body["cursor"] is None
        assert len(body["items"]) == 3

    def test_full_loop_covers_every_case_exactly_once(
        self, client, analyst_token,
    ):
        for i in range(7):
            _open_case(customer_po_number=f"PO-{i:03d}")
        collected: list[str] = []
        cursor: str | None = None
        # Mirror the asoe-ui `do { fetch } while (cursor)` loop.
        for _ in range(20):  # belt-and-braces; should resolve in 4 iters at limit=2.
            url = "/api/v1/cases?limit=2"
            if cursor:
                url += f"&cursor={cursor}"
            r = client.get(url, headers=_auth(analyst_token))
            body = r.json()
            collected.extend(item["case_id"] for item in body["items"])
            if not body["has_more"]:
                break
            cursor = body["cursor"]
        assert len(collected) == 7
        assert len(set(collected)) == 7

    def test_unknown_cursor_falls_through_to_first_page(
        self, client, analyst_token,
    ):
        # Grandfathered behaviour — stale cursor tokens from a
        # previous browser session must not 500 or 422.
        _open_case(customer_po_number="PO-001")
        r = client.get(
            "/api/v1/cases?cursor=case-deleted-or-never-existed",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 1

    def test_cursor_pagination_respects_filters(
        self, client, analyst_token,
    ):
        # Two CUSTOMER cases + three API cases, page the CUSTOMER subset
        # at limit=1.
        for i in range(2):
            _open_case(
                customer_po_number=f"M-{i:03d}",
                origin="CUSTOMER",
                source_channel="email",
            )
        for i in range(3):
            _open_case(customer_po_number=f"A-{i:03d}", origin="API")

        url = "/api/v1/cases?origin=CUSTOMER&limit=1"
        r = client.get(url, headers=_auth(analyst_token))
        body = r.json()
        assert body["total"] == 2
        assert len(body["items"]) == 1
        assert body["has_more"] is True

        r2 = client.get(url + f"&cursor={body['cursor']}", headers=_auth(analyst_token))
        body2 = r2.json()
        assert body2["has_more"] is False
        assert len(body2["items"]) == 1
        # Items across the two pages must be disjoint.
        assert body["items"][0]["case_id"] != body2["items"][0]["case_id"]


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
        assert body["origin"] == "API"

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
            origin="CUSTOMER",
            source_channel="email",
        )
        r = client.get("/api/v1/cases", headers=_auth(assigned_token))
        ids = {item["case_id"] for item in r.json()["items"]}
        assert case.case_id in ids

    def test_partner_detail_visible_post_s15a_alignment(
        self, client, partner_token,
    ):
        # S15a alignment 2026-05-12 — GET /api/v1/cases/{id} now
        # matches GET /api/v1/exceptions/{id}: tenant-scoped only,
        # no account/role-based scope filter. The case-list endpoint
        # still applies `_scope_to_user` so a partner's queue stays
        # filtered to their own retailer; this lock asserts the
        # detail endpoint is reachable when the partner has the
        # case_id (e.g. via a notification deep-link).
        case = _open_case(customer_po_number="PO-NO-WMT")
        _attach_child(order_id="TARGET-999", parent_case_id=case.case_id)
        r = client.get(
            f"/api/v1/cases/{case.case_id}",
            headers=_auth(partner_token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["case_id"] == case.case_id


# ---------------------------------------------------------------------------
# Detail-path visibility invariant (S15a follow-on)
# ---------------------------------------------------------------------------

class TestDetailVisibilityInvariant:
    """`visible(exception) → visible(parent_case)` on the detail path.

    The S15a pivot makes `/cases/{id}?record=<id>` the canonical action
    surface. An operator who can read a child record via
    `GET /api/v1/exceptions/{id}` must also be able to read its parent
    case — otherwise a notification or deep-link to an exception lands
    on a parent that 404s, leaving the operator unable to access the
    HITL ribbon.

    The invariant tests the asymmetry that #152 fixed: if either
    endpoint regrows a scope filter (e.g. `_scope_to_user` creeps back
    onto `/cases/{id}`), this test fails on the role/scope combination
    where divergence appears.
    """

    @pytest.fixture()
    def seeded_universe(self, client):
        """Mix of cases × child records that span the scope axes we
        care about: account_id assigned vs not; retailer_id matching
        vs not; case-derived `customer_id` scope vs child-derived."""
        # Account "acc-001" — in the assigned analyst's allowlist.
        case_in = _open_case(customer_po_number="PO-IN", customer_id="acc-001")
        rec_in = _attach_child(
            order_id="WALMART-IN", parent_case_id=case_in.case_id,
            account_id="acc-001",
        )
        # Account "acc-999" — out of allowlist; retailer matches partner.
        case_out = _open_case(customer_po_number="PO-OUT", customer_id="acc-999")
        rec_out = _attach_child(
            order_id="WALMART-OUT", parent_case_id=case_out.case_id,
            account_id="acc-999",
        )
        # Case with no customer_id; child carries no account either —
        # purely tenant-scoped, no further constraints.
        case_bare = _open_case(customer_po_number="PO-BARE")
        rec_bare = _attach_child(
            order_id="TARGET-BARE", parent_case_id=case_bare.case_id,
        )
        return [
            (case_in, rec_in),
            (case_out, rec_out),
            (case_bare, rec_bare),
        ]

    @pytest.mark.parametrize("token_fixture", [
        "analyst_token", "manager_token", "viewer_token",
        "assigned_token", "partner_token",
    ])
    def test_visible_exception_implies_visible_case(
        self, client, seeded_universe, token_fixture, request,
    ):
        token = request.getfixturevalue(token_fixture)
        for case, record in seeded_universe:
            exc_resp = client.get(
                f"/api/v1/exceptions/{record.id}", headers=_auth(token),
            )
            if exc_resp.status_code != 200:
                continue
            case_resp = client.get(
                f"/api/v1/cases/{case.case_id}", headers=_auth(token),
            )
            assert case_resp.status_code == 200, (
                f"detail-path asymmetry under {token_fixture}: "
                f"exception {record.id} visible but parent case "
                f"{case.case_id} returned {case_resp.status_code} — "
                f"a `_scope_to_user`-shaped filter has regressed onto "
                f"/api/v1/cases/{{id}}."
            )
