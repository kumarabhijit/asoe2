"""ADR-042 inbox section coverage — a vanilla MANUAL_ORDER_INTAKE record going
through `/resolve` against the sandbox-registered stubs must produce a
`/analysis` response with EVERY Customer Inbox tab populated, not just the one
seeded by `/api/v1/_sandbox/seed/email-attachment-anchors`.

This is the wiring contract between
  * recipes/registry.py (the gateway dependencies on ManualOrderIntakeRecipe)
  * api/sandbox_gateways.py (the registered stubs)
  * api/profile_composer.py (the composer projections)

If any tab is empty in real-API mode for an arbitrary inbox case, this test
fails — making the Azure pre-prod gap visible at red-green.
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


def _auth():
    return {"Authorization": f"Bearer {create_test_token(roles=['analyst'], org='tenant-a')}"}


def _resolve_manual_intake(client: TestClient, suffix: str = "INB-1") -> str:
    """Drive one vanilla MANUAL_ORDER_INTAKE event through /resolve so the
    sandbox stubs populate the enrichment_context the way Azure pre-prod
    will once the real platform connectors land."""
    r = client.post(
        "/api/v1/exceptions/resolve",
        json={
            "order_id": f"EML-PO-INBOX-{suffix}",
            "line_item": 1,
            "po_price": 100.0,
            "sap_base_price": 100.0,
            "event_type": "MANUAL_ORDER_INTAKE",
            "retailer_id": "acct-southeast-distrib",
            "line_count": 1,
            "metadata": {
                "composite_confidence": 0.97,
                "non_disableable_floor": {
                    "sender_authorized": True,
                    "customer_resolved": True,
                    "duplicate_po_clear": True,
                    "credit_clear": True,
                },
                "validation_failures": [],
            },
        },
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    return r.json()["exception_id"]


def _get_analysis(client: TestClient, exc_id: str) -> dict:
    r = client.get(f"/api/v1/exceptions/{exc_id}/analysis", headers=_auth())
    assert r.status_code == 200, r.text
    return r.json()


def test_inbox_record_has_email_source(client):
    exc_id = _resolve_manual_intake(client, "EMAIL")
    analysis = _get_analysis(client, exc_id)
    src = analysis.get("email_source")
    assert src is not None, f"email_source missing; keys={list(analysis)}"
    assert src["from_address"]
    assert len(src["body_hash"]) == 64


def test_inbox_record_has_email_order_entry_analysis(client):
    exc_id = _resolve_manual_intake(client, "EOEA")
    analysis = _get_analysis(client, exc_id)
    assert analysis.get("email_order_entry_analysis") is not None


def test_inbox_record_has_entities_analysis(client):
    # `extract_entities` stub on `order_extraction` lands at
    # enrichment_context["inbox_entities"]; composer projects to entities_analysis.
    exc_id = _resolve_manual_intake(client, "ENT")
    analysis = _get_analysis(client, exc_id)
    ent = analysis.get("entities_analysis")
    assert ent is not None, f"entities_analysis missing; keys={list(analysis)}"
    assert isinstance(ent["extracted"], list) and ent["extracted"]


def test_inbox_record_has_sap_data_analysis(client):
    # `sap_order.validate` stub lands at enrichment_context["sap_data"];
    # composer requires system + validation_status to project.
    exc_id = _resolve_manual_intake(client, "SAP")
    analysis = _get_analysis(client, exc_id)
    sap = analysis.get("sap_data_analysis")
    assert sap is not None, f"sap_data_analysis missing; keys={list(analysis)}"
    assert sap["system"] and sap["validation_status"]


def test_inbox_record_has_order_entry_extraction(client):
    # `extract_order` stub on `order_extraction` lands at
    # enrichment_context["order_entry_extraction"].
    exc_id = _resolve_manual_intake(client, "OE")
    analysis = _get_analysis(client, exc_id)
    oee = analysis.get("order_entry_extraction")
    assert oee is not None, f"order_entry_extraction missing; keys={list(analysis)}"
    assert oee["header"]["customer_po"]
    assert oee["line_items"]


def test_inbox_record_has_edi_850_audit(client):
    exc_id = _resolve_manual_intake(client, "EDI")
    analysis = _get_analysis(client, exc_id)
    edi = analysis.get("edi_850_audit")
    assert edi is not None, f"edi_850_audit missing; keys={list(analysis)}"


def test_inbox_record_has_change_analysis(client):
    exc_id = _resolve_manual_intake(client, "CHG")
    analysis = _get_analysis(client, exc_id)
    chg = analysis.get("change_analysis")
    assert chg is not None, f"change_analysis missing; keys={list(analysis)}"


def test_inbox_record_has_knowledge_graph(client):
    exc_id = _resolve_manual_intake(client, "KG")
    analysis = _get_analysis(client, exc_id)
    kg = analysis.get("knowledge_graph")
    assert kg is not None, f"knowledge_graph missing; keys={list(analysis)}"
    assert kg["nodes"]


def test_all_inbox_sections_populated_in_single_record(client):
    """The Azure pre-prod contract: one record → all inbox sections present."""
    exc_id = _resolve_manual_intake(client, "ALL")
    analysis = _get_analysis(client, exc_id)
    missing = [
        k for k in (
            "email_source", "email_order_entry_analysis", "entities_analysis",
            "sap_data_analysis", "order_entry_extraction", "edi_850_audit",
            "change_analysis", "knowledge_graph",
        )
        if analysis.get(k) is None
    ]
    assert not missing, f"inbox sections missing on a vanilla intake record: {missing}"
