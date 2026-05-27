"""PARITY-0 Phase 0b — non-sandbox boot must have a deterministic gateway state.

ASOE_ENV=preprod boots with the stub gateways (initially a thin re-use of the
sandbox set; real connectors land per Phase 6). The contract enforced here:

* `register_preprod_gateways()` exists and is callable.
* It registers the same audit-bearing gateways the sandbox set registers
  (`email_intake`, `order_extraction`, `sap_order`, `sap_doc`, `oms`, …) so
  the recipe registry resolves.
* `create_app()` under ASOE_ENV=preprod runs the dispatcher and the
  resulting registry is non-empty.
* A `/resolve` MANUAL_ORDER_INTAKE call under preprod produces a /analysis
  response with the inbox sections populated — same contract that
  `test_e2e_manual_order_intake_inbox_sections.py` locks for sandbox.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.deps import create_test_token
from api.store import exception_store
from gateways import registry


def _auth(env: str = "preprod"):
    token = create_test_token(roles=["analyst"], org="tenant-a", env=env)
    return {"Authorization": f"Bearer {token}"}


def test_register_preprod_gateways_exists_and_callable():
    from api.preprod_gateways import register_preprod_gateways
    assert callable(register_preprod_gateways)


def test_preprod_registration_populates_the_required_gateway_set(monkeypatch):
    monkeypatch.setenv("ASOE_ENV", "preprod")
    registry.clear_registry()
    from api.preprod_gateways import register_preprod_gateways
    register_preprod_gateways()
    names = set(registry.registered_gateways())
    # The five gateways the ManualOrderIntakeRecipe depends on must all
    # resolve under preprod (per recipes/registry.py declarations).
    for required in (
        "email_intake", "order_extraction", "sap_order",
        "edi_850", "change_analysis", "knowledge_graph",
    ):
        assert required in names, f"preprod gateway set missing: {required}"


def test_preprod_registration_is_idempotent(monkeypatch):
    monkeypatch.setenv("ASOE_ENV", "preprod")
    registry.clear_registry()
    from api.preprod_gateways import register_preprod_gateways
    register_preprod_gateways()
    first = set(registry.registered_gateways())
    register_preprod_gateways()
    assert set(registry.registered_gateways()) == first


def test_app_boot_under_preprod_env_registers_gateways(monkeypatch):
    monkeypatch.setenv("ASOE_ENV", "preprod")
    registry.clear_registry()
    from api.app import create_app
    create_app()
    assert "email_intake" in registry.registered_gateways()


def test_preprod_with_graph_driver_swaps_email_intake_to_router(monkeypatch):
    """PARITY-6.1 — ``ASOE_EMAIL_INTAKE_DRIVER=graph`` replaces the
    ``email_intake`` StubGateway with the ``GraphIntakeGateway`` live-
    or-stub router. The registry name is preserved so existing recipe
    dependencies resolve unchanged; the routing decision is per-request
    (canary % + driver env) so this test only locks the swap, not the
    per-call routing (covered by tests/test_msgraph_intake.py)."""
    monkeypatch.setenv("ASOE_ENV", "preprod")
    monkeypatch.setenv("ASOE_EMAIL_INTAKE_DRIVER", "graph")
    # The live backend requires Graph creds; provide stubs so the
    # factory doesn't refuse to construct. The router only calls the
    # factory on the first request that's canary-eligible — this test
    # never makes a request, so the creds are only needed for type
    # safety when the factory eventually runs.
    monkeypatch.setenv("ASOE_GRAPH_TENANT_ID", "tenant-stub")
    monkeypatch.setenv("ASOE_GRAPH_CLIENT_ID", "client-stub")
    monkeypatch.setenv("ASOE_GRAPH_CLIENT_SECRET", "secret-stub")
    registry.clear_registry()
    from api.preprod_gateways import register_preprod_gateways
    from gateways.msgraph_intake import GraphIntakeGateway
    register_preprod_gateways()
    resolved = registry.get_gateway("email_intake")
    assert isinstance(resolved, GraphIntakeGateway), (
        "ASOE_EMAIL_INTAKE_DRIVER=graph must route email_intake through "
        "the live-or-stub router"
    )


def test_preprod_resolve_populates_inbox_sections(monkeypatch):
    """The contract from asoe2 #175 (sandbox) must also hold for preprod:
    a vanilla MANUAL_ORDER_INTAKE event through /resolve produces an
    /analysis response with email_source + entities + sap_data +
    order_entry_extraction + edi_850_audit + change_analysis +
    knowledge_graph all populated."""
    monkeypatch.setenv("ASOE_ENV", "preprod")
    registry.clear_registry()
    exception_store.clear()
    from api.app import create_app
    client = TestClient(create_app(), raise_server_exceptions=False)

    r = client.post(
        "/api/v1/exceptions/resolve",
        json={
            "order_id": "EML-PO-PREPROD-INB-1",
            "line_item": 1,
            "po_price": 100.0,
            "sap_base_price": 100.0,
            "event_type": "MANUAL_ORDER_INTAKE",
            "retailer_id": "acct-southeast-distrib",
            "line_count": 1,
            "metadata": {
                "composite_confidence": 0.97,
                "non_disableable_floor": {
                    "sender_authorized": True, "customer_resolved": True,
                    "duplicate_po_clear": True, "credit_clear": True,
                },
                "validation_failures": [],
            },
        },
        headers=_auth(env="preprod"),
    )
    assert r.status_code == 200, r.text
    exc_id = r.json()["exception_id"]

    rr = client.get(
        f"/api/v1/exceptions/{exc_id}/analysis",
        headers=_auth(env="preprod"),
    )
    assert rr.status_code == 200, rr.text
    analysis = rr.json()
    missing = [
        k for k in (
            "email_source", "email_order_entry_analysis", "entities_analysis",
            "sap_data_analysis", "order_entry_extraction", "edi_850_audit",
            "change_analysis", "knowledge_graph",
        )
        if analysis.get(k) is None
    ]
    assert not missing, (
        f"inbox sections missing under ASOE_ENV=preprod: {missing}. "
        f"register_preprod_gateways must register the same audit-bearing "
        f"set sandbox does until Phase 6 real connectors land."
    )
