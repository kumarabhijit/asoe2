"""Sandbox-inbound isolation sentinel (ADR-042 gate #8, re-homed to Phase 7).

ADR-042 §2.2 / §5c (Compliance veto): the sandbox inbound injector
(`POST /api/v1/_sandbox/seed/manual-order-intake`) MUST be hard-isolated from
production — sandbox-injected records cannot exist in prod, cannot leak across
tenants, and cannot append to the prod audit (hash) chain. The env-gate 403 is
already locked in tests/contract/test_sandbox_manual_order_intake_producer.py;
this sentinel locks the audit-chain + cross-tenant dimensions that complete the
gate.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.store import exception_store


@pytest.fixture()
def client():
    exception_store.clear()
    return TestClient(create_app(), raise_server_exceptions=False)


def _auth(roles, org):
    return {"Authorization": f"Bearer {create_test_token(roles=roles, org=org)}"}


def _seed(client: TestClient, order_id: str, org: str):
    return client.post(
        "/api/v1/_sandbox/seed/manual-order-intake",
        json={"order_id": order_id},
        headers=_auth(["analyst"], org),
    )


def test_sandbox_seed_does_not_append_to_prod_audit_chain(client: TestClient):
    # The injector is fixture wiring, not a SOX state transition — it must not
    # write to the immutable policy audit (hash) chain.
    before = list(exception_store.get_audit_log("tenant-a"))
    res = _seed(client, "EML-ISO-1", "tenant-a")
    assert res.status_code == 200, res.text
    after = exception_store.get_audit_log("tenant-a")
    # No new audit-chain events from the seed.
    assert len(after) == len(before)
    # The chain (whatever its prior state) remains valid — uncontaminated.
    valid, _break = exception_store.verify_audit_chain("tenant-a")
    assert valid is True


def test_sandbox_seeded_record_is_tenant_scoped(client: TestClient):
    # A record injected under tenant-a must be invisible to another tenant —
    # it cannot "acquire" a different (prod) tenant's scope.
    res = _seed(client, "EML-ISO-2", "tenant-a")
    assert res.status_code == 200, res.text
    exception_id = res.json()["exception_id"]
    assert exception_store.get(exception_id, "tenant-a") is not None
    assert exception_store.get(exception_id, "tenant-b") is None
