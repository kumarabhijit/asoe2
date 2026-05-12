"""Contract: GET /api/v1/exceptions/{id} surfaces parent_case_id (S15a follow-on).

Locks the missing field discovered while wiring asoe-ui's S15a
exceptionUrl() helper. The case-centric pivot in ADR-038 §H.3
stamps `parent_case_id` on every materialised record, but the
detail-response schema didn't carry the field — only the queue
list did. The asoe-ui browser specs that look up
/cases/<parent>?record=<id> from a per-record handle therefore
saw `parent_case_id: undefined` and threw at fixture-helper time.

This test pins the round-trip end-to-end:
  1. Materialise a record via /exceptions/resolve.
  2. GET /api/v1/exceptions/{id} and read parent_case_id.
  3. Assert it matches the record's stamped parent_case_id and
     resolves to a real OrderCase in /api/v1/cases.

Per the S15a amendment 2026-05-12, every persisted record now
materialises a case — including clean-COMPLETE Automated Orders
that were previously Tier-1 stateless. The post-amendment
contract: parent_case_id is always set, and always resolves.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.store import exception_store


@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
def client(app):
    exception_store.clear()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def manager_token() -> str:
    return create_test_token(roles=["manager"], org="tenant-a")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_parent_case_id_present_when_case_materialises(
    client: TestClient, manager_token: str,
) -> None:
    # An Automated Order that lands MANUAL_REVIEW_REQUIRED → case
    # materialises lazily. The stamped parent_case_id must surface
    # on the detail response.
    res = client.post(
        "/api/v1/exceptions/resolve",
        json={
            "order_id": "PO-PARENT-CASE-1",
            "po_price": 100.0,
            "sap_base_price": 120.0,
            "event_type": "EDI_850_PRICE_MISMATCH",
        },
        headers=_auth(manager_token),
    )
    assert res.status_code == 200, res.text
    exc_id = res.json()["exception_id"]

    detail = client.get(
        f"/api/v1/exceptions/{exc_id}",
        headers=_auth(manager_token),
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert "parent_case_id" in body, (
        "ExceptionDetailResponse must carry parent_case_id — see "
        "asoe-ui S15a (exceptionUrl helper depends on it)."
    )
    case_id = body["parent_case_id"]

    # If a case materialised, asserting on the field gives the UI a
    # bookmarkable /cases/<id>?record=<exception_id> URL. The case
    # MUST resolve via GET /api/v1/cases — a stale/dangling id here
    # is the bug class the S15a UI flow would surface as a 404.
    if case_id is not None:
        case_res = client.get(
            f"/api/v1/cases/{case_id}",
            headers=_auth(manager_token),
        )
        assert case_res.status_code == 200, (
            f"parent_case_id {case_id!r} on exception {exc_id!r} "
            f"must resolve to a real case via /api/v1/cases."
        )


def test_clean_complete_records_also_materialise_post_s15a(
    client: TestClient, manager_token: str,
) -> None:
    # S15a amendment 2026-05-12: every persisted record now
    # materialises a case, including clean-COMPLETE Automated
    # Orders. The post-amendment contract: parent_case_id is always
    # set, and the asoe-ui exceptionUrl() helper can rely on it
    # being a real, fetchable case_id.
    #
    # Pre-amendment this same payload landed parent_case_id=None
    # (Tier-1 stateless). See `api/case_resolver.py` section header
    # for the policy history.
    res = client.post(
        "/api/v1/exceptions/resolve",
        json={
            "order_id": "PO-S15A-CLEAN",
            "po_price": 100.0,
            "sap_base_price": 100.0,
            "event_type": "EDI_850_PRICE_MISMATCH",
        },
        headers=_auth(manager_token),
    )
    assert res.status_code == 200, res.text
    exc_id = res.json()["exception_id"]

    detail = client.get(
        f"/api/v1/exceptions/{exc_id}",
        headers=_auth(manager_token),
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    parent_case_id = body.get("parent_case_id")
    assert parent_case_id is not None, (
        "Post-S15a, clean-COMPLETE Automated Orders must still "
        "materialise a case (see ADR-038 §7.2 amendment)."
    )
    case_res = client.get(
        f"/api/v1/cases/{parent_case_id}",
        headers=_auth(manager_token),
    )
    assert case_res.status_code == 200, (
        f"parent_case_id {parent_case_id!r} on a clean-COMPLETE "
        f"record must resolve to a real case."
    )


def test_summary_and_detail_agree_on_parent_case_id(
    client: TestClient, manager_token: str,
) -> None:
    # The same field surfaces on the queue list (ExceptionSummary)
    # and the detail (ExceptionDetailResponse). The two views must
    # not diverge — otherwise a UI that joins them gets ambiguous
    # routing.
    res = client.post(
        "/api/v1/exceptions/resolve",
        json={
            "order_id": "PO-SUMMARY-PARITY",
            "po_price": 100.0,
            "sap_base_price": 120.0,
            "event_type": "EDI_850_PRICE_MISMATCH",
        },
        headers=_auth(manager_token),
    )
    assert res.status_code == 200, res.text
    exc_id = res.json()["exception_id"]

    list_res = client.get(
        "/api/v1/exceptions",
        headers=_auth(manager_token),
    )
    assert list_res.status_code == 200, list_res.text
    summary = next(
        (row for row in list_res.json()["data"] if row["id"] == exc_id),
        None,
    )
    assert summary is not None, "expected the created exception on the queue"

    detail_res = client.get(
        f"/api/v1/exceptions/{exc_id}",
        headers=_auth(manager_token),
    )
    assert detail_res.status_code == 200, detail_res.text
    assert summary["parent_case_id"] == detail_res.json()["parent_case_id"]
