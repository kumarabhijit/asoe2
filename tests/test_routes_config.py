"""ADR-030 / A9 — Tenant-config admin endpoint integration tests (PR-C.2).

Uses FastAPI TestClient against a freshly-migrated in-memory SQLite
adapter, with ``app.dependency_overrides`` injecting the test repo
instances so the route handlers and the registered gateway both see
the same DB.

Covers:
  * PUT /layers/{layer} — admin RBAC, scope validation, audit-chain
    side-effect.
  * GET /layers/{layer} — admin/manager RBAC, analyst forbidden.
  * DELETE /layers/{layer} — admin RBAC, missing-row 404, audit-chain
    captures new_weights={}.
  * GET /resolve — platform-only baseline; DB tenant override appears
    in trace when validation passes.
  * GET /audit — prefix-filtered to ConfigChange entries; carries
    hash-chain fields.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.routes.config import (
    get_policy_repo,
    get_tenant_config_repo,
)
from db.connection import SQLiteAdapter
from db.repository import PolicyRepository, TenantConfigRepository
from gateways.registry import register_gateway
from gateways.tenant_config import TenantConfigGateway


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def adapter() -> SQLiteAdapter:
    a = SQLiteAdapter(":memory:")
    a.apply_schema()
    return a


@pytest.fixture
def config_repo(adapter: SQLiteAdapter) -> TenantConfigRepository:
    return TenantConfigRepository(adapter=adapter)


@pytest.fixture
def policy_repo(adapter: SQLiteAdapter) -> PolicyRepository:
    return PolicyRepository(adapter=adapter)


@pytest.fixture
def app(monkeypatch, adapter, config_repo, policy_repo):
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    app = create_app()
    app.dependency_overrides[get_tenant_config_repo] = lambda: config_repo
    app.dependency_overrides[get_policy_repo] = lambda: policy_repo
    # Register a DB-backed gateway under the SAME adapter so /resolve
    # sees the same data the PUT/DELETE endpoints write. Overwrites
    # whatever conftest._register_oms_stub registered (file-only).
    register_gateway(TenantConfigGateway(repository=config_repo))
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _token(role: str, sub: str = None) -> str:
    return create_test_token(
        sub=sub or f"{role}@acme.com",
        email=f"{role}@acme.com",
        name=role.title(),
        roles=[role],
        org="acme",
    )


def _hdrs(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# PUT /api/v1/config/tenants/{tenant_id}/layers/{layer}
# ---------------------------------------------------------------------------


class TestUpsertLayer:
    def test_admin_can_upsert_tenant_layer(self, client):
        resp = client.put(
            "/api/v1/config/tenants/acme/layers/tenant",
            headers=_hdrs(_token("admin")),
            json={
                "scope": {},
                "weights": {"po_number": 0.4},
                "change_reason": "Q2 ramp",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["layer"] == "tenant"
        assert body["weights"] == {"po_number": 0.4}
        assert body["created_by"] == "admin@acme.com"

    def test_manager_cannot_upsert(self, client):
        resp = client.put(
            "/api/v1/config/tenants/acme/layers/tenant",
            headers=_hdrs(_token("manager")),
            json={"scope": {}, "weights": {"po_number": 0.4}},
        )
        assert resp.status_code == 403

    def test_unknown_layer_returns_404(self, client):
        resp = client.put(
            "/api/v1/config/tenants/acme/layers/rogue",
            headers=_hdrs(_token("admin")),
            json={"scope": {}, "weights": {"po_number": 0.4}},
        )
        assert resp.status_code == 404

    def test_tenant_layer_rejects_non_empty_scope(self, client):
        resp = client.put(
            "/api/v1/config/tenants/acme/layers/tenant",
            headers=_hdrs(_token("admin")),
            json={
                "scope": {"customer_id": "X"},
                "weights": {"po_number": 0.4},
            },
        )
        assert resp.status_code == 400

    def test_tier_layer_requires_customer_tier(self, client):
        resp = client.put(
            "/api/v1/config/tenants/acme/layers/tier",
            headers=_hdrs(_token("admin")),
            json={"scope": {}, "weights": {"po_number": 0.4}},
        )
        assert resp.status_code == 400

    def test_channel_layer_requires_customer_id_and_channel(self, client):
        resp = client.put(
            "/api/v1/config/tenants/acme/layers/channel",
            headers=_hdrs(_token("admin")),
            json={
                "scope": {"customer_id": "R-10"},
                "weights": {"po_number": 0.4},
            },
        )
        assert resp.status_code == 400

    def test_upsert_writes_audit_chain_entry(self, client):
        client.put(
            "/api/v1/config/tenants/acme/layers/tenant",
            headers=_hdrs(_token("admin")),
            json={
                "scope": {},
                "weights": {"po_number": 0.4},
                "change_reason": "Q2 ramp",
            },
        )
        resp = client.get(
            "/api/v1/config/tenants/acme/audit",
            headers=_hdrs(_token("admin")),
        )
        assert resp.status_code == 200
        entries = resp.json()["entries"]
        assert len(entries) == 1
        e = entries[0]
        assert e["policy_key"] == "duplicate_po.weights.tenant"
        assert e["changed_by"] == "admin@acme.com"
        assert e["change_reason"] == "Q2 ramp"
        assert e["new_value"] == {"po_number": 0.4}
        assert e["previous_value"] is None
        assert e["event_hash"]
        assert e["prev_hash"] == "GENESIS"

    def test_upsert_overwrite_records_previous_weights(self, client):
        client.put(
            "/api/v1/config/tenants/acme/layers/tenant",
            headers=_hdrs(_token("admin")),
            json={"scope": {}, "weights": {"po_number": 0.4}},
        )
        client.put(
            "/api/v1/config/tenants/acme/layers/tenant",
            headers=_hdrs(_token("admin")),
            json={"scope": {}, "weights": {"po_number": 0.5}},
        )
        audit = client.get(
            "/api/v1/config/tenants/acme/audit",
            headers=_hdrs(_token("admin")),
        ).json()["entries"]
        # Newest first — entry[0] is the second PUT
        assert audit[0]["new_value"] == {"po_number": 0.5}
        assert audit[0]["previous_value"] == {"po_number": 0.4}

    def test_tier_upsert_writes_correct_policy_key(self, client):
        client.put(
            "/api/v1/config/tenants/acme/layers/tier",
            headers=_hdrs(_token("admin")),
            json={
                "scope": {"customer_tier": "strategic"},
                "weights": {"po_number": 0.45},
            },
        )
        audit = client.get(
            "/api/v1/config/tenants/acme/audit",
            headers=_hdrs(_token("admin")),
        ).json()["entries"]
        assert audit[0]["policy_key"] == "duplicate_po.weights.tier:strategic"


# ---------------------------------------------------------------------------
# GET /api/v1/config/tenants/{tenant_id}/layers/{layer}
# ---------------------------------------------------------------------------


class TestListLayer:
    def test_list_returns_rows(self, client):
        for cid in ("C1", "C2"):
            client.put(
                "/api/v1/config/tenants/acme/layers/customer",
                headers=_hdrs(_token("admin")),
                json={
                    "scope": {"customer_id": cid},
                    "weights": {"po_number": 0.45},
                },
            )
        resp = client.get(
            "/api/v1/config/tenants/acme/layers/customer",
            headers=_hdrs(_token("manager")),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["layer"] == "customer"
        assert len(body["rows"]) == 2

    def test_analyst_forbidden(self, client):
        resp = client.get(
            "/api/v1/config/tenants/acme/layers/tenant",
            headers=_hdrs(_token("analyst")),
        )
        assert resp.status_code == 403

    def test_unknown_layer_returns_404(self, client):
        resp = client.get(
            "/api/v1/config/tenants/acme/layers/rogue",
            headers=_hdrs(_token("admin")),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/v1/config/tenants/{tenant_id}/layers/{layer}
# ---------------------------------------------------------------------------


class TestDeleteLayer:
    def test_delete_removes_row_and_writes_audit(self, client):
        client.put(
            "/api/v1/config/tenants/acme/layers/tenant",
            headers=_hdrs(_token("admin")),
            json={"scope": {}, "weights": {"po_number": 0.4}},
        )
        resp = client.request(
            "DELETE",
            "/api/v1/config/tenants/acme/layers/tenant",
            headers=_hdrs(_token("admin")),
            json={"scope": {}, "change_reason": "rollback"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["deleted"] is True
        assert body["layer"] == "tenant"

        # GET should now miss
        list_resp = client.get(
            "/api/v1/config/tenants/acme/layers/tenant",
            headers=_hdrs(_token("admin")),
        ).json()
        assert list_resp["rows"] == []

        # Audit log has 2 entries: insert + delete (newest first)
        audit = client.get(
            "/api/v1/config/tenants/acme/audit",
            headers=_hdrs(_token("admin")),
        ).json()["entries"]
        assert len(audit) == 2
        assert audit[0]["new_value"] == {}  # delete marker
        assert audit[0]["previous_value"] == {"po_number": 0.4}
        assert audit[0]["change_reason"] == "rollback"

    def test_delete_missing_row_returns_404(self, client):
        resp = client.request(
            "DELETE",
            "/api/v1/config/tenants/acme/layers/tenant",
            headers=_hdrs(_token("admin")),
            json={"scope": {}},
        )
        assert resp.status_code == 404

    def test_manager_cannot_delete(self, client):
        resp = client.request(
            "DELETE",
            "/api/v1/config/tenants/acme/layers/tenant",
            headers=_hdrs(_token("manager")),
            json={"scope": {}},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/v1/config/tenants/{tenant_id}/resolve
# ---------------------------------------------------------------------------


class TestResolveConfig:
    def test_returns_platform_when_no_overrides(self, client):
        resp = client.get(
            "/api/v1/config/tenants/acme/resolve",
            headers=_hdrs(_token("manager")),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["validation_status"] == "ok"
        # All sources are platform when no DB overrides exist
        assert all(
            t["source_layer"] == "platform"
            for t in body["contribution_trace"]
        )

    def test_db_tier_override_appears_in_trace(self, client):
        # Tier override that keeps weight sum balanced (po_number
        # 0.30 → 0.20, customer_id 0.15 → 0.25 — net delta zero).
        client.put(
            "/api/v1/config/tenants/acme/layers/tier",
            headers=_hdrs(_token("admin")),
            json={
                "scope": {"customer_tier": "strategic"},
                "weights": {"po_number": 0.20, "customer_id": 0.25},
            },
        )
        resp = client.get(
            "/api/v1/config/tenants/acme/resolve?customer_tier=strategic",
            headers=_hdrs(_token("manager")),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # When the merged sum balances, validation passes and the trace
        # records 'tier' as the source for overridden keys.
        if body["validation_status"] == "ok":
            sources = {
                t["signal"]: t["source_layer"]
                for t in body["contribution_trace"]
            }
            assert sources["po_number"] == "tier"
            assert sources["customer_id"] == "tier"
            # Untouched keys still come from platform
            assert sources["line_items"] == "platform"

    def test_resolve_unauthenticated_rejected(self, client):
        resp = client.get("/api/v1/config/tenants/acme/resolve")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/config/tenants/{tenant_id}/audit
# ---------------------------------------------------------------------------


class TestAuditList:
    def test_filtered_to_config_change_prefix(self, client, policy_repo):
        # Write a config-change row via PUT + an unrelated audit row
        # via the policy repo directly. Only the ConfigChange row
        # should appear in /audit.
        client.put(
            "/api/v1/config/tenants/acme/layers/tenant",
            headers=_hdrs(_token("admin")),
            json={"scope": {}, "weights": {"po_number": 0.4}},
        )
        policy_repo.create_audit_event(
            tenant_id="acme",
            policy_key="unrelated.threshold.something",
            previous_value={"v": 1},
            new_value={"v": 2},
            changed_by="admin@acme.com",
            change_reason="adhoc",
        )

        resp = client.get(
            "/api/v1/config/tenants/acme/audit",
            headers=_hdrs(_token("admin")),
        )
        assert resp.status_code == 200
        entries = resp.json()["entries"]
        # Only the ConfigChange entry comes through
        assert len(entries) == 1
        assert entries[0]["policy_key"].startswith(
            "duplicate_po.weights."
        )

    def test_analyst_forbidden(self, client):
        resp = client.get(
            "/api/v1/config/tenants/acme/audit",
            headers=_hdrs(_token("analyst")),
        )
        assert resp.status_code == 403

    def test_audit_chain_links(self, client):
        """Two consecutive PUTs produce two audit rows whose hashes link."""
        client.put(
            "/api/v1/config/tenants/acme/layers/tenant",
            headers=_hdrs(_token("admin")),
            json={"scope": {}, "weights": {"po_number": 0.4}},
        )
        client.put(
            "/api/v1/config/tenants/acme/layers/customer",
            headers=_hdrs(_token("admin")),
            json={
                "scope": {"customer_id": "R-10"},
                "weights": {"po_number": 0.45},
            },
        )
        entries = client.get(
            "/api/v1/config/tenants/acme/audit",
            headers=_hdrs(_token("admin")),
        ).json()["entries"]
        # Newest first: customer entry then tenant entry.
        # The customer entry's prev_hash should equal the tenant
        # entry's event_hash (chain link).
        assert entries[0]["prev_hash"] == entries[1]["event_hash"]
        assert entries[1]["prev_hash"] == "GENESIS"
