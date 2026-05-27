"""GET /api/v1/cases/{case_id}/classification-history.

Requirements §8.6 / acceptance criterion #9 — the classification audit
trail surfaces to operators via the case detail panel. The endpoint is
read-only; writes go through CaseStore.update / record_classification.
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
    return create_test_token(roles=["analyst"], org="tenant-a")


@pytest.fixture()
def partner_token():
    return create_test_token(roles=["partner"], org="tenant-a")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _open(po: str = "PO-1", supergroup_code: str | None = "SG_NEW_ORDER") -> str:
    case, _ = case_store.lookup_or_create(
        tenant_id="tenant-a", origin="CUSTOMER", source_channel="email",
        customer_po_number=po, supergroup_code=supergroup_code,
    )
    return case.case_id


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_endpoint_returns_intake_event(client, analyst_token):
    """An intake with a supergroup_code auto-writes one history row;
    the endpoint surfaces it."""
    case_id = _open(supergroup_code="SG_NEW_ORDER")
    r = client.get(
        f"/api/v1/cases/{case_id}/classification-history",
        headers=_auth(analyst_token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["case_id"] == case_id
    assert item["supergroup_code"] == "SG_NEW_ORDER"
    assert item["classifier_type"] == "RULE"
    assert item["classified_by"] == "system:case_intake"
    assert item["taxonomy_version"]  # stamped, non-empty


def test_endpoint_returns_empty_for_unclassified_case(client, analyst_token):
    case_id = _open(supergroup_code=None)
    r = client.get(
        f"/api/v1/cases/{case_id}/classification-history",
        headers=_auth(analyst_token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_endpoint_returns_reclassification_chain_in_order(client, analyst_token):
    """Multiple classifications stack in append order (oldest first).
    Locks the audit-readability promise."""
    case_id = _open(supergroup_code="SG_NEW_ORDER")
    case_store.update(
        case_id, supergroup_code="SG_NEEDS_TRIAGE",
        classified_by="user:csr-1", classifier_type="HUMAN",
        reason_text="Cannot identify intent",
    )
    case_store.update(
        case_id, supergroup_code="SG_ORDER_CHANGE",
        classified_by="user:lead-1", classifier_type="HUMAN",
        reason_text="Lead re-triaged",
    )

    r = client.get(
        f"/api/v1/cases/{case_id}/classification-history",
        headers=_auth(analyst_token),
    )
    body = r.json()
    assert body["total"] == 3
    sgs = [it["supergroup_code"] for it in body["items"]]
    assert sgs == ["SG_NEW_ORDER", "SG_NEEDS_TRIAGE", "SG_ORDER_CHANGE"]
    # The HUMAN reason_text round-trips for the steward UI.
    assert body["items"][1]["reason_text"] == "Cannot identify intent"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_unknown_case_returns_404(client, analyst_token):
    r = client.get(
        "/api/v1/cases/does-not-exist/classification-history",
        headers=_auth(analyst_token),
    )
    assert r.status_code == 404


def test_cross_tenant_case_returns_404(client, analyst_token):
    """Tenant scoping: a case opened in tenant-b is invisible from
    tenant-a even with a valid token (same as /cases/{id})."""
    case, _ = case_store.lookup_or_create(
        tenant_id="tenant-b", origin="CUSTOMER", source_channel="email",
        customer_po_number="PO-OTHER", supergroup_code="SG_NEW_ORDER",
    )
    r = client.get(
        f"/api/v1/cases/{case.case_id}/classification-history",
        headers=_auth(analyst_token),
    )
    assert r.status_code == 404


def test_unauthenticated_request_rejected(client):
    case_id = _open()
    r = client.get(f"/api/v1/cases/{case_id}/classification-history")
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------

def test_partner_role_sees_redacted_reason_text(client, partner_token):
    """Partners (external retailers) see the structural audit trail
    but not operator-authored free text in ``reason_text`` (may
    contain internal commercial notes)."""
    case_id = _open(supergroup_code="SG_NEW_ORDER")
    case_store.update(
        case_id, supergroup_code="SG_ORDER_CHANGE",
        classified_by="user:csr-1", classifier_type="HUMAN",
        reason_text="Escalation risk — internal flag",
    )
    r = client.get(
        f"/api/v1/cases/{case_id}/classification-history",
        headers=_auth(partner_token),
    )
    assert r.status_code == 200
    items = r.json()["items"]
    # Every row arrives with reason_text=None for the partner role,
    # even when the underlying event has a value.
    assert all(it["reason_text"] is None for it in items)
    # Structural audit is preserved.
    assert items[1]["classified_by"] == "user:csr-1"
    assert items[1]["classifier_type"] == "HUMAN"
    assert items[1]["supergroup_code"] == "SG_ORDER_CHANGE"


def test_analyst_role_sees_reason_text(client, analyst_token):
    """Internal roles (analyst / manager / admin / viewer) see the
    full audit trail including reason_text."""
    case_id = _open(supergroup_code="SG_NEW_ORDER")
    case_store.update(
        case_id, supergroup_code="SG_ORDER_CHANGE",
        classified_by="user:csr-1", classifier_type="HUMAN",
        reason_text="Escalation risk — internal flag",
    )
    r = client.get(
        f"/api/v1/cases/{case_id}/classification-history",
        headers=_auth(analyst_token),
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert items[1]["reason_text"] == "Escalation risk — internal flag"


def test_response_carries_full_audit_shape(client, analyst_token):
    """Every field declared on ClassificationHistoryEntry surfaces in
    the JSON response — if a future commit drops one, the UI breaks
    and this test catches it."""
    case_id = _open(supergroup_code="SG_NEW_ORDER")
    case_store.update(
        case_id, supergroup_code="SG_ORDER_CHANGE",
        intent_code_classification="INT_MANUAL_ORDER_INTAKE",
        classified_by="system:email_classifier",
        classifier_type="MODEL",
        model_version="2026-05-claude",
        reason_text="High-confidence reclassification",
        source_event_id="evt-1",
    )
    r = client.get(
        f"/api/v1/cases/{case_id}/classification-history",
        headers=_auth(analyst_token),
    )
    body = r.json()
    # Second row is the MODEL reclassification.
    model_row = body["items"][1]
    assert {
        "id", "case_id", "child_case_id", "supergroup_code", "intent_code",
        "classified_at", "classified_by", "classifier_type", "model_version",
        "reason_text", "source_event_id", "taxonomy_version",
    }.issubset(model_row.keys())
    assert model_row["classifier_type"] == "MODEL"
    assert model_row["model_version"] == "2026-05-claude"
    assert model_row["intent_code"] == "INT_MANUAL_ORDER_INTAKE"
