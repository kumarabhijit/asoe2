"""Tests for the Playwright-fixture sandbox endpoints."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.store import exception_store


@pytest.fixture()
def client():
    exception_store.clear()
    # Audit log clear too, so verify_audit_chain starts from GENESIS.
    if hasattr(exception_store, "_audit_log"):
        exception_store._audit_log.clear()
    return TestClient(create_app(), raise_server_exceptions=False)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_pending(client, token):
    r = client.post(
        "/api/v1/exceptions/resolve/explain",
        json={
            "order_id": "PO-SB-1",
            "po_price": 100.0,
            "sap_base_price": 120.0,
            "event_type": "EDI_850_PRICE_MISMATCH",
        },
        headers=_auth(token),
    )
    return r.json()["exception_id"]


class TestSeedFinancialImpact:
    def test_attaches_financial_impact_to_existing_record(self, client):
        analyst = create_test_token(roles=["analyst"], org="tenant-a")
        manager = create_test_token(roles=["manager"], org="tenant-a")
        eid = _seed_pending(client, analyst)
        r = client.post(
            "/api/v1/_sandbox/seed/financial-impact",
            json={"exception_id": eid, "financial_impact_usd": 25_000.0},
            headers=_auth(manager),
        )
        assert r.status_code == 200, r.json()
        assert r.json()["financial_impact_usd"] == 25_000.0
        rec = exception_store.get(eid, "tenant-a")
        assert rec.resolution_data["financial_impact_usd"] == 25_000.0

    def test_unknown_exception_returns_404(self, client):
        manager = create_test_token(roles=["manager"], org="tenant-a")
        r = client.post(
            "/api/v1/_sandbox/seed/financial-impact",
            json={"exception_id": "does-not-exist", "financial_impact_usd": 1000.0},
            headers=_auth(manager),
        )
        assert r.status_code == 404

    def test_analyst_forbidden(self, client):
        analyst = create_test_token(roles=["analyst"], org="tenant-a")
        eid = _seed_pending(client, analyst)
        r = client.post(
            "/api/v1/_sandbox/seed/financial-impact",
            json={"exception_id": eid, "financial_impact_usd": 1.0},
            headers=_auth(analyst),
        )
        assert r.status_code == 403

    def test_sandbox_endpoints_hidden_outside_sandbox_env(self, monkeypatch):
        """A production-configured app must not expose sandbox routes."""
        monkeypatch.setenv("ASOE_ENV", "production")
        # Re-create app after env change so the include_router guard fires.
        app = create_app()
        with TestClient(app, raise_server_exceptions=False) as prod_client:
            admin = create_test_token(
                roles=["admin"], org="tenant-a", env="production",
            )
            r = prod_client.post(
                "/api/v1/_sandbox/seed/financial-impact",
                json={"exception_id": "x", "financial_impact_usd": 1.0},
                headers=_auth(admin),
            )
            assert r.status_code == 404, (
                "sandbox routes must not be mounted in production"
            )


class TestTenantReset:
    def test_clears_caller_tenant(self, client):
        analyst = create_test_token(roles=["analyst"], org="tenant-a")
        admin = create_test_token(roles=["admin"], org="tenant-a")
        # Produce a record and an audit event
        eid = _seed_pending(client, analyst)
        client.post(
            f"/api/v1/exceptions/{eid}/escalate",
            json={"reason": "pre-reset"},
            headers=_auth(analyst),
        )
        assert exception_store.get(eid, "tenant-a") is not None

        r = client.post(
            "/api/v1/_sandbox/tenant/reset",
            json={},
            headers=_auth(admin),
        )
        assert r.status_code == 200, r.json()
        assert r.json()["ok"] is True
        # Records and audit entries for tenant-a are gone.
        assert exception_store.get(eid, "tenant-a") is None
        tenant_a_audit = [
            e for e in getattr(exception_store, "_audit_log", [])
            if e.get("tenant_id") == "tenant-a"
        ]
        assert tenant_a_audit == []

    def test_cross_tenant_reset_forbidden(self, client):
        admin_a = create_test_token(roles=["admin"], org="tenant-a")
        r = client.post(
            "/api/v1/_sandbox/tenant/reset",
            json={"tenant_id": "tenant-b"},
            headers=_auth(admin_a),
        )
        assert r.status_code == 403
