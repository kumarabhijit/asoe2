"""Phase 3 — end-to-end classification + audit flow.

Single test scenario walking the full lifecycle:

  1. A CUSTOMER email arrives — case opens with provisional
     supergroup_code from the email classifier (MODEL).
  2. The CSR reviews and reclassifies (HUMAN).
  3. The supergroup is briefly NEEDS_TRIAGE; attempt to RESOLVE
     during that window is hard-blocked (§8.2).
  4. A lead corrects the classification to a real super-group.
  5. The case resolves cleanly.
  6. The classification-history endpoint surfaces all four events
     in append order with their respective classifier_type stamps.

Ties together acceptance criteria #5, #9, and §8.3 reclassification
rights (HUMAN at CSR / lead level + MODEL at intake).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.store import NeedsTriageCloseBlocked, case_store, exception_store


@pytest.fixture()
def client():
    case_store.clear()
    exception_store.clear()
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def analyst_token():
    return create_test_token(roles=["analyst"], org="tenant-a")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_customer_email_classification_lifecycle(client, analyst_token):
    # --- 1. Intake — MODEL classifier ran at the edge -------------------
    case, opened = case_store.lookup_or_create(
        tenant_id="tenant-a", origin="CUSTOMER", source_channel="email",
        customer_po_number="PO-E2E-1",
        supergroup_code="SG_ORDER_CHANGE",
        intent_code="INT_MANUAL_ORDER_INTAKE",
        classified_by="system:email_classifier",
        classifier_type="MODEL",
    )
    assert opened is True
    case_id = case.case_id

    # --- 2. CSR re-reads the email and corrects the classification -----
    case_store.update(
        case_id, supergroup_code="SG_NEW_ORDER",
        classified_by="user:csr-1", classifier_type="HUMAN",
        reason_text="Customer was placing a new order, not amending",
    )

    # --- 3. CSR escalates — can't decide between two paths -------------
    case_store.update(
        case_id, supergroup_code="SG_NEEDS_TRIAGE",
        classified_by="user:csr-1", classifier_type="HUMAN",
        reason_text="Order touches both new and change semantics; needs lead",
    )

    # --- §8.2 hard-block kicks in: cannot resolve while NEEDS_TRIAGE ---
    with pytest.raises(NeedsTriageCloseBlocked):
        case_store.update(case_id, status="RESOLVED")

    # --- 4. Lead re-triages back to a real super-group -----------------
    case_store.update(
        case_id, supergroup_code="SG_NEW_ORDER",
        classified_by="user:lead-1", classifier_type="HUMAN",
        reason_text="Treating as new order; change request would be separate PO",
    )

    # --- 5. Now the resolve goes through ------------------------------
    final = case_store.update(case_id, status="RESOLVED")
    assert final.status == "RESOLVED"
    assert final.supergroup_code == "SG_NEW_ORDER"

    # --- 6. Audit trail surfaces all four classification events --------
    r = client.get(
        f"/api/v1/cases/{case_id}/classification-history",
        headers=_auth(analyst_token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 4

    rows = body["items"]
    # MODEL intake -> HUMAN correct -> HUMAN escalate -> HUMAN re-triage
    assert [r["classifier_type"] for r in rows] == [
        "MODEL", "HUMAN", "HUMAN", "HUMAN",
    ]
    assert [r["supergroup_code"] for r in rows] == [
        "SG_ORDER_CHANGE", "SG_NEW_ORDER", "SG_NEEDS_TRIAGE", "SG_NEW_ORDER",
    ]
    assert rows[0]["classified_by"] == "system:email_classifier"
    assert rows[1]["classified_by"] == "user:csr-1"
    assert rows[3]["classified_by"] == "user:lead-1"

    # Every event was stamped with the active taxonomy version (same
    # version because there was no taxonomy change between the steps).
    versions = {r["taxonomy_version"] for r in rows}
    assert len(versions) == 1
    assert next(iter(versions))  # non-empty
