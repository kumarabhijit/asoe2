"""GET /api/v1/cases V5.1.1 extensions (Phase 28.5.x §D1-D3, D7).

Pins:

  * `/api/v1/health` surfaces `allowed_case_statuses` and
    `allowed_case_sources` so the UI's CaseListPane filter chips
    drive off `useHealth` per Guardrail #1.
  * `/api/v1/cases` response items carry the derived
    `child_intents` array (Phase 28.5.x §D2).
  * Multi-value status filter (any-match).
  * Multi-value intents filter (any-match against child intents).
  * `since=` preset filter (today / 24h / 7d / 30d).
  * `q=` free-text fuzzy match across case_id /
    customer_po_number / sales_order_id / customer_id.
  * `asoe_cases_returned_p99` SLI gauge updates per request
    (Phase 28.5.x §D7 pagination re-open trigger).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import case_intents_cache
from api.app import create_app
from api.deps import create_test_token
from api.metrics import (
    cases_returned_snapshot,
    reset_cases_returned_window,
)
from api.pubsub import event_publisher
from api.store import case_store, exception_store


@pytest.fixture(autouse=True)
def _reset():
    case_intents_cache.clear()
    case_store.clear()
    if hasattr(exception_store, "clear"):
        exception_store.clear()
    if hasattr(event_publisher, "clear"):
        event_publisher.clear()
    reset_cases_returned_window()
    yield
    case_intents_cache.clear()
    case_store.clear()
    reset_cases_returned_window()


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


def _seed(
    tenant_id: str,
    *,
    po: str,
    status: str = "OPEN_AGENT_PROCESSING",
    origin: str = "CUSTOMER",
    customer_id: str = "acct-walmart",
    sales_order_id: str | None = None,
    child_intent: str | None = "CONTRACTUAL_CORRECTION",
    opened_at: str | None = None,
) -> str:
    case, _ = case_store.lookup_or_create(
        tenant_id=tenant_id,
        origin=origin,  # type: ignore[arg-type]
        source_channel="email" if origin == "CUSTOMER" else "edi_x12_850",
        customer_id=customer_id,
        customer_po_number=po,
        sales_order_id=sales_order_id,
    )
    if status != case.status or opened_at:
        update_fields: dict = {"status": status}
        if opened_at:
            update_fields["opened_at"] = opened_at
        case_store.update(case.case_id, **update_fields)
    if child_intent:
        exception_store.create(
            tenant_id=tenant_id,
            order_id=po,
            event_type="EDI_850_PRICE_MISMATCH",
            trace_id=f"trace-{po}",
            intent=child_intent,
            shadow_verdict="GREEN",
            parent_case_id=case.case_id,
        )
    return case.case_id


# ---------------------------------------------------------------------------
# /api/v1/health — D1
# ---------------------------------------------------------------------------


class TestHealthAllowedCaseVocab:
    def test_health_surfaces_allowed_case_statuses(self, client):
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        body = r.json()
        assert set(body["allowed_case_statuses"]) >= {
            "OPEN_AGENT_PROCESSING",
            "OPEN_AWAITING_HUMAN",
            "OPEN_AWAITING_BUYER",
            "OPEN_AWAITING_ERP",
            "RESOLVED",
            "FAILED",
            "BLOCKED",
        }

    def test_health_surfaces_allowed_case_origins(self, client):
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        assert set(r.json()["allowed_case_origins"]) == {
            "CUSTOMER", "API",
        }


# ---------------------------------------------------------------------------
# /api/v1/cases — D2 (child_intents in response)
# ---------------------------------------------------------------------------


class TestCasesChildIntents:
    def test_list_carries_child_intents_per_case(self, client, manager_token):
        _seed("tenant-a", po="PO-1", child_intent="DUPLICATE_PO")
        _seed("tenant-a", po="PO-2", child_intent="CONTRACTUAL_CORRECTION")
        r = client.get("/api/v1/cases", headers=_auth(manager_token))
        assert r.status_code == 200
        items = r.json()["items"]
        intents_by_po = {
            item["customer_po_number"]: item["child_intents"] for item in items
        }
        assert intents_by_po["PO-1"] == ["DUPLICATE_PO"]
        assert intents_by_po["PO-2"] == ["CONTRACTUAL_CORRECTION"]

    def test_get_single_case_also_carries_child_intents(
        self, client, manager_token,
    ):
        cid = _seed("tenant-a", po="PO-S", child_intent="DUPLICATE_PO")
        r = client.get(f"/api/v1/cases/{cid}", headers=_auth(manager_token))
        assert r.status_code == 200
        assert r.json()["child_intents"] == ["DUPLICATE_PO"]


# ---------------------------------------------------------------------------
# /api/v1/cases query params — D1 (status multi), D2 (intents), D3 (since/q)
# ---------------------------------------------------------------------------


class TestCasesFilters:
    def test_multi_value_status_filter_any_match(self, client, manager_token):
        _seed("tenant-a", po="PO-Live", status="OPEN_AGENT_PROCESSING")
        _seed("tenant-a", po="PO-Wait", status="OPEN_AWAITING_HUMAN")
        _seed("tenant-a", po="PO-Done", status="RESOLVED")
        r = client.get(
            "/api/v1/cases",
            headers=_auth(manager_token),
            params={"status": "OPEN_AGENT_PROCESSING,OPEN_AWAITING_HUMAN"},
        )
        assert r.status_code == 200
        pos = {i["customer_po_number"] for i in r.json()["items"]}
        assert pos == {"PO-Live", "PO-Wait"}

    def test_intents_filter_any_match_against_children(
        self, client, manager_token,
    ):
        _seed("tenant-a", po="PO-DUP", child_intent="DUPLICATE_PO")
        _seed("tenant-a", po="PO-CC",  child_intent="CONTRACTUAL_CORRECTION")
        _seed("tenant-a", po="PO-BO",  child_intent="BACK_ORDER_OOS")
        r = client.get(
            "/api/v1/cases",
            headers=_auth(manager_token),
            params={"intents": "DUPLICATE_PO,BACK_ORDER_OOS"},
        )
        assert r.status_code == 200
        pos = {i["customer_po_number"] for i in r.json()["items"]}
        assert pos == {"PO-DUP", "PO-BO"}

    def test_q_param_substring_matches_across_fields(
        self, client, manager_token,
    ):
        _seed("tenant-a", po="ACME-PO-001", customer_id="acct-acme")
        _seed("tenant-a", po="OTHER-PO", customer_id="acct-walmart")
        # Match on customer_po_number substring
        r = client.get(
            "/api/v1/cases",
            headers=_auth(manager_token),
            params={"q": "acme"},
        )
        assert r.status_code == 200
        pos = {i["customer_po_number"] for i in r.json()["items"]}
        # Both "ACME-PO-001" (po number) and "acct-acme" (customer_id)
        # contain "acme" as substring.
        assert "ACME-PO-001" in pos

    def test_since_preset_filters_recent_cases(self, client, manager_token):
        # Seed one recent, one old.
        _seed(
            "tenant-a", po="PO-RECENT",
            opened_at="2099-01-01T00:00:00+00:00",  # synthetic future date
        )
        _seed(
            "tenant-a", po="PO-OLD",
            opened_at="2020-01-01T00:00:00+00:00",
        )
        r = client.get(
            "/api/v1/cases",
            headers=_auth(manager_token),
            params={"since": "7d"},
        )
        assert r.status_code == 200
        pos = {i["customer_po_number"] for i in r.json()["items"]}
        assert "PO-RECENT" in pos
        assert "PO-OLD" not in pos

    def test_unrecognised_since_preset_falls_through_unfiltered(
        self, client, manager_token,
    ):
        # Compliance veto on silent truncation: an unrecognised
        # preset must NOT zero the list.
        _seed("tenant-a", po="PO-A")
        r = client.get(
            "/api/v1/cases",
            headers=_auth(manager_token),
            params={"since": "garbage"},
        )
        assert r.status_code == 200
        assert len(r.json()["items"]) == 1


# ---------------------------------------------------------------------------
# /api/v1/metrics — D7 (cases_returned SLI)
# ---------------------------------------------------------------------------


class TestCasesReturnedSli:
    def test_sli_records_payload_size_per_request(self, client, manager_token):
        for i in range(5):
            _seed("tenant-a", po=f"PO-{i}")
        # Empty window before first request.
        assert cases_returned_snapshot() == (0, 0.0, 0, 0)
        r = client.get("/api/v1/cases", headers=_auth(manager_token))
        assert r.status_code == 200
        samples, avg, p95, p99 = cases_returned_snapshot()
        assert samples == 1
        assert avg == 5.0
        assert p95 == 5
        assert p99 == 5

    def test_metrics_endpoint_emits_cases_returned_gauges(
        self, client, manager_token,
    ):
        _seed("tenant-a", po="PO-A")
        client.get("/api/v1/cases", headers=_auth(manager_token))
        r = client.get("/api/v1/metrics")
        body = r.text
        assert "asoe_cases_returned_samples 1" in body
        assert "asoe_cases_returned_p99" in body
        # The metric MUST be present even when no /cases requests
        # have happened — start at zero so the dashboard panel
        # always has a baseline.
        reset_cases_returned_window()
        r2 = client.get("/api/v1/metrics")
        assert "asoe_cases_returned_samples 0" in r2.text
        assert "asoe_cases_returned_p99 0" in r2.text
