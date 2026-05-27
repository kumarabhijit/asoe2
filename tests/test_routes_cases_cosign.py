"""ADR-040 X.0 — case-level four-eyes / cosign endpoint tests.

Locks the X.0 wire-up:
  * Both endpoints return 404 when `ASOE_CASE_COSIGN_ENABLED` is
    unset (default off — ratification is a config flip).
  * `POST /cases/{id}/override` parks `pending_override` on the
    case and transitions status → OPEN_AWAITING_HUMAN.
  * Second initiator hitting a case in PENDING_COSIGN gets 409
    (forward-only invariant per ADR-040 §4).
  * `POST /cases/{id}/override/cosign`:
    * SoD — initiator can't cosign their own.
    * Notes mandatory.
    * approve=True → status RESOLVED.
    * approve=False → status OPEN_AGENT_PROCESSING.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.store import case_store, exception_store


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    case_store.clear()
    exception_store.clear()
    yield
    case_store.clear()
    exception_store.clear()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ASOE_CASE_COSIGN_ENABLED", "1")
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def disabled_client(monkeypatch):
    monkeypatch.delenv("ASOE_CASE_COSIGN_ENABLED", raising=False)
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def manager_a_token():
    return create_test_token(
        roles=["manager"], org="tenant-a", sub="user-A",
    )


@pytest.fixture
def manager_b_token():
    return create_test_token(
        roles=["manager"], org="tenant-a", sub="user-B",
    )


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _open_case(tenant_id: str = "tenant-a"):
    case, _ = case_store.lookup_or_create(
        tenant_id=tenant_id,
        origin="CUSTOMER",
        source_channel="email",
        customer_po_number="PO-COSIGN-1",
    )
    return case


def _init_payload(**overrides):
    body = {
        "pending_action": "APPROVE_DESPITE_DOWNGRADE",
        "pending_reason_tag": "customer_opt_out",
        "aggregate_financial_impact_usd": 12_000.0,
        "child_exception_ids": ["ex-1", "ex-2"],
        "notes": "Operator authorised; LLM downgrade overridden.",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Default-off behaviour (ratification gate)
# ---------------------------------------------------------------------------

class TestFlagOffReturns404:
    def test_initiate_404(self, disabled_client, manager_a_token):
        case = _open_case()
        r = disabled_client.post(
            f"/api/v1/cases/{case.case_id}/override",
            json=_init_payload(),
            headers=_auth(manager_a_token),
        )
        assert r.status_code == 404

    def test_cosign_404(self, disabled_client, manager_a_token):
        case = _open_case()
        r = disabled_client.post(
            f"/api/v1/cases/{case.case_id}/override/cosign",
            json={"approve": True, "notes": "ok"},
            headers=_auth(manager_a_token),
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Initiate
# ---------------------------------------------------------------------------

class TestInitiate:
    def test_parks_pending_override(self, client, manager_a_token):
        case = _open_case()
        r = client.post(
            f"/api/v1/cases/{case.case_id}/override",
            json=_init_payload(),
            headers=_auth(manager_a_token),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["pending_override"] is not None
        assert body["pending_override"]["initiator"] == "user-A"
        assert body["pending_override"]["pending_action"] == "APPROVE_DESPITE_DOWNGRADE"
        assert body["status"] == "OPEN_AWAITING_HUMAN"

    def test_second_initiator_409s(self, client, manager_a_token, manager_b_token):
        case = _open_case()
        client.post(
            f"/api/v1/cases/{case.case_id}/override",
            json=_init_payload(),
            headers=_auth(manager_a_token),
        )
        # Same case, second user tries to initiate again.
        r2 = client.post(
            f"/api/v1/cases/{case.case_id}/override",
            json=_init_payload(),
            headers=_auth(manager_b_token),
        )
        assert r2.status_code == 409

    def test_unknown_case_404(self, client, manager_a_token):
        r = client.post(
            "/api/v1/cases/does-not-exist/override",
            json=_init_payload(),
            headers=_auth(manager_a_token),
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Cosign — approve / reject / SoD / notes
# ---------------------------------------------------------------------------

class TestCosign:
    def _seed_pending(self, client, manager_a_token):
        case = _open_case()
        client.post(
            f"/api/v1/cases/{case.case_id}/override",
            json=_init_payload(),
            headers=_auth(manager_a_token),
        )
        return case

    def test_approve_resolves(self, client, manager_a_token, manager_b_token):
        case = self._seed_pending(client, manager_a_token)
        r = client.post(
            f"/api/v1/cases/{case.case_id}/override/cosign",
            json={"approve": True, "notes": "Reviewed and approved."},
            headers=_auth(manager_b_token),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "RESOLVED"
        assert body["pending_override"] is None

    def test_reject_restores(self, client, manager_a_token, manager_b_token):
        case = self._seed_pending(client, manager_a_token)
        r = client.post(
            f"/api/v1/cases/{case.case_id}/override/cosign",
            json={"approve": False, "notes": "Insufficient justification."},
            headers=_auth(manager_b_token),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "OPEN_AGENT_PROCESSING"
        assert body["pending_override"] is None

    def test_initiator_self_cosign_403(self, client, manager_a_token):
        case = self._seed_pending(client, manager_a_token)
        r = client.post(
            f"/api/v1/cases/{case.case_id}/override/cosign",
            json={"approve": True, "notes": "Trying to self-cosign."},
            headers=_auth(manager_a_token),
        )
        assert r.status_code == 403
        # The error envelope shape varies; just confirm the
        # explanation is somewhere in the body.
        assert "Segregation of duties" in r.text

    def test_empty_notes_422(self, client, manager_a_token, manager_b_token):
        case = self._seed_pending(client, manager_a_token)
        r = client.post(
            f"/api/v1/cases/{case.case_id}/override/cosign",
            json={"approve": True, "notes": "   "},
            headers=_auth(manager_b_token),
        )
        assert r.status_code == 422

    def test_no_pending_override_409(self, client, manager_b_token):
        case = _open_case()
        # Skip the initiate step — nothing to cosign.
        r = client.post(
            f"/api/v1/cases/{case.case_id}/override/cosign",
            json={"approve": True, "notes": "ok"},
            headers=_auth(manager_b_token),
        )
        assert r.status_code == 409

    def test_cross_tenant_404(self, client, manager_a_token):
        # User on tenant-a tries to cosign a tenant-b case.
        case = _open_case(tenant_id="tenant-b")
        r = client.post(
            f"/api/v1/cases/{case.case_id}/override/cosign",
            json={"approve": True, "notes": "ok"},
            headers=_auth(manager_a_token),
        )
        assert r.status_code == 404
