"""ADR-041 P3e §3.1 + §3.4 — list-endpoint CaseSummary projection
and RBAC `dollar_impact` strip tests.

Verifies:
  * GET /api/v1/cases items carry the seven new CaseSummary fields.
  * `dollar_impact` is populated for callers with
    `exceptions:approve` or `exceptions:override`.
  * `dollar_impact` is null for callers without either (viewer role).
  * Other CaseSummary fields (audit_verdict_color, intent,
    customer_name) are NOT stripped — only the financial number.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.store import case_store, exception_store


@pytest.fixture()
def client():
    case_store.clear()
    exception_store.clear()
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def analyst_token():
    """Analyst — has `exceptions:approve`; should see dollar_impact."""
    return create_test_token(roles=["analyst"], org="tenant-a")


@pytest.fixture()
def manager_token():
    """Manager — has `exceptions:override`; should see dollar_impact."""
    return create_test_token(roles=["manager"], org="tenant-a")


@pytest.fixture()
def viewer_token():
    """Viewer — has `exceptions:read` only; dollar_impact MUST
    be stripped (ADR-041 P3e §3.4)."""
    return create_test_token(roles=["viewer"], org="tenant-a")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _setup_case_with_dollar_impact(
    *,
    tenant_id: str = "tenant-a",
    customer_id: str = "acme-corp",
    impact_usd: float = 4147.20,
    shadow_verdict: str = "RED",
    intent: str = "PRICE_DISCREPANCY",
):
    """Common fixture: a case with one child carrying both a
    shadow_verdict (drives audit_verdict_color) and a
    financial_impact_usd (drives dollar_impact)."""
    case, _ = case_store.lookup_or_create(
        tenant_id,
        origin="API",
        source_channel="edi_x12_850",
        customer_po_number="PO-X",
        customer_id=customer_id,
    )
    exception_store.create(
        tenant_id=tenant_id,
        order_id="PO-X",
        event_type="EDI_850",
        trace_id="trace-PO-X",
        intent=intent,
        shadow_verdict=shadow_verdict,
        resolution_data={"financial_impact_usd": impact_usd},
        parent_case_id=case.case_id,
    )
    return case


# ---------------------------------------------------------------------------
# CaseSummary projection — list endpoint shape
# ---------------------------------------------------------------------------


class TestCaseSummaryProjection:
    def test_list_items_carry_all_seven_projection_fields(
        self, client, analyst_token
    ):
        _setup_case_with_dollar_impact()
        r = client.get("/api/v1/cases", headers=_auth(analyst_token))
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        row = items[0]
        for key in (
            "customer_name",
            "top_line_sku_code",
            "top_line_sku_title",
            "problem_one_liner",
            "intent",
            "dollar_impact",
            "audit_verdict_color",
        ):
            assert key in row, f"missing CaseSummary field {key}"

    def test_audit_verdict_color_rolls_up_from_children(
        self, client, analyst_token
    ):
        _setup_case_with_dollar_impact(shadow_verdict="RED")
        r = client.get("/api/v1/cases", headers=_auth(analyst_token))
        assert r.json()["items"][0]["audit_verdict_color"] == "R"

    def test_intent_projects_from_primary_child(self, client, analyst_token):
        _setup_case_with_dollar_impact(intent="DUPLICATE_PO")
        r = client.get("/api/v1/cases", headers=_auth(analyst_token))
        assert r.json()["items"][0]["intent"] == "DUPLICATE_PO"

    def test_customer_name_projects_from_customer_id(
        self, client, analyst_token
    ):
        _setup_case_with_dollar_impact(customer_id="acme-corp")
        r = client.get("/api/v1/cases", headers=_auth(analyst_token))
        assert r.json()["items"][0]["customer_name"] == "acme-corp"

    def test_dollar_impact_carries_amount_cents_and_iso_currency(
        self, client, analyst_token
    ):
        _setup_case_with_dollar_impact(impact_usd=4147.20)
        r = client.get("/api/v1/cases", headers=_auth(analyst_token))
        impact = r.json()["items"][0]["dollar_impact"]
        assert impact == {"amount_cents": 414720, "currency": "USD"}

    def test_per_intent_template_fields_are_null_for_now(
        self, client, analyst_token
    ):
        """Recipe-team-owned fields ship null until the per-intent
        templates land. NEVER synthesised from event metadata."""
        _setup_case_with_dollar_impact()
        r = client.get("/api/v1/cases", headers=_auth(analyst_token))
        row = r.json()["items"][0]
        assert row["top_line_sku_code"] is None
        assert row["top_line_sku_title"] is None
        assert row["problem_one_liner"] is None


# ---------------------------------------------------------------------------
# RBAC — dollar_impact stripping
# ---------------------------------------------------------------------------


class TestDollarImpactRbac:
    def test_analyst_sees_dollar_impact(self, client, analyst_token):
        _setup_case_with_dollar_impact()
        r = client.get("/api/v1/cases", headers=_auth(analyst_token))
        assert r.json()["items"][0]["dollar_impact"] is not None

    def test_manager_sees_dollar_impact(self, client, manager_token):
        _setup_case_with_dollar_impact()
        r = client.get("/api/v1/cases", headers=_auth(manager_token))
        assert r.json()["items"][0]["dollar_impact"] is not None

    def test_viewer_dollar_impact_is_stripped(self, client, viewer_token):
        """Viewer lacks both `exceptions:approve` and
        `exceptions:override`; ADR-041 P3e §3.4 strips
        `dollar_impact` at the route boundary."""
        _setup_case_with_dollar_impact()
        r = client.get("/api/v1/cases", headers=_auth(viewer_token))
        row = r.json()["items"][0]
        # The field IS present in the response shape; only the
        # value is null. UI's EvidenceBlock collapses the cell.
        assert "dollar_impact" in row
        assert row["dollar_impact"] is None

    def test_viewer_still_sees_other_summary_fields(
        self, client, viewer_token
    ):
        """The RBAC strip is targeted — only dollar_impact. The
        verdict color, intent, and customer name remain visible
        per the Compliance review (not privileged data)."""
        _setup_case_with_dollar_impact(
            shadow_verdict="RED",
            intent="PRICE_DISCREPANCY",
            customer_id="acme-corp",
        )
        r = client.get("/api/v1/cases", headers=_auth(viewer_token))
        row = r.json()["items"][0]
        assert row["audit_verdict_color"] == "R"
        assert row["intent"] == "PRICE_DISCREPANCY"
        assert row["customer_name"] == "acme-corp"


# ---------------------------------------------------------------------------
# Single-case GET — same projection
# ---------------------------------------------------------------------------


class TestSingleCaseDetailProjection:
    def test_get_case_carries_projection_fields(
        self, client, analyst_token
    ):
        """The /cases/{id} detail endpoint shares `_serialise_case`
        with /cases, so the seven projection fields land here too.
        Tested separately because the failure mode is asymmetric —
        a regression in one path doesn't necessarily break the other."""
        case = _setup_case_with_dollar_impact()
        r = client.get(
            f"/api/v1/cases/{case.case_id}", headers=_auth(analyst_token)
        )
        assert r.status_code == 200
        row = r.json()
        assert row["audit_verdict_color"] == "R"
        assert row["intent"] == "PRICE_DISCREPANCY"
        assert row["dollar_impact"] == {
            "amount_cents": 414720,
            "currency": "USD",
        }
