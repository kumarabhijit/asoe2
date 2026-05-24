"""ADR-042 Phase 4 — DRAFT_REPLY disposition (compose a buyer reply).

An explicit DRAFT_REPLY disposition re-enters the graph (Shadow → ReplyDraftRecipe)
to compose a buyer reply draft from the reviewed extraction + operator reply
params. The draft is persisted to resolution_data.reply_draft; the record stays
in review (a draft is not a resolution). No email is sent — the send is a
separate authorised action.

Written test-first.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.routes.exceptions import _clear_idempotency_cache
from api.store import exception_store

_EXTRACTION = {
    "source_type": "PDF", "confidence": 0.78,
    "header": {"customer_po": "0093847612", "order_type": "ZOR",
               "sales_org": "1000", "dist_channel": "10"},
    "customer_name": "Walmart Stores Inc", "customer_bp": "300001",
    "line_items": [{"line_num": "001", "material": "BEV-COLA-12PK",
                    "quantity": 480, "uom": "CS", "unit_price": 8.64}],
    "validation_flags": ["Requested delivery date is missing.",
                         "Unit of measure could not be confirmed."],
}


@pytest.fixture()
def client():
    exception_store.clear()
    _clear_idempotency_cache()
    return TestClient(create_app(), raise_server_exceptions=False)


def _auth(sub="user-A"):
    return {"Authorization": f"Bearer {create_test_token(roles=['analyst'], org='tenant-a', sub=sub)}"}


def _seed(*, intent="MANUAL_ORDER_INTAKE", extraction=_EXTRACTION):
    return exception_store.create(
        tenant_id="tenant-a", order_id="EML-PO-1", event_type="MANUAL_ORDER_INTAKE",
        trace_id="tr-1", intent=intent, shadow_verdict="YELLOW",
        final_status="MANUAL_REVIEW_REQUIRED",
        resolution_data={"recommended_action": "REQUEST_CLARIFICATION"},
        original_event={"order_id": "EML-PO-1", "event_type": "MANUAL_ORDER_INTAKE",
                        "po_price": 0.0, "sap_base_price": 0.0, "retailer_id": "R-1"},
        enrichment_context={"order_entry_extraction": extraction},
    )


def _draft(client, rec_id, **body):
    payload = {"action": "DRAFT_REPLY", "reason_tag": "OTHER", "notes": "drafting reply"}
    payload.update(body)
    return client.patch(f"/api/v1/exceptions/{rec_id}/disposition",
                        json=payload, headers=_auth())


def test_draft_reply_composes_and_persists(client) -> None:
    rec = _seed()
    r = _draft(client, rec.id, reply={"recipient": "orders@walmart.example"})
    assert r.status_code == 200, r.text
    stored = exception_store.get(rec.id, "tenant-a")
    rd = stored.resolution_data["reply_draft"]
    assert rd["status"] == "DRAFTED"
    draft = rd["draft"]
    assert draft["recipient"] == "orders@walmart.example"
    assert "0093847612" in draft["subject"]  # PO interpolated
    assert "Walmart Stores Inc" in draft["body"]  # customer name
    # validation_flags became the clarification bullet list
    assert "- Requested delivery date is missing." in draft["body"]
    assert rd["drafted_by"] == "user-A"


def test_draft_reply_does_not_resolve_the_record(client) -> None:
    rec = _seed()
    before = exception_store.get(rec.id, "tenant-a").lifecycle_state
    _draft(client, rec.id, reply={"recipient": "orders@walmart.example"})
    after = exception_store.get(rec.id, "tenant-a").lifecycle_state
    assert after == before  # a draft is not a resolution
    assert after != "RESOLVED"


def test_operator_edits_override_the_draft(client) -> None:
    rec = _seed()
    r = _draft(client, rec.id, reply={
        "recipient": "orders@walmart.example",
        "edits": {"subject": "Quick question on PO 0093847612"},
    })
    assert r.status_code == 200, r.text
    rd = exception_store.get(rec.id, "tenant-a").resolution_data["reply_draft"]
    assert rd["draft"]["subject"] == "Quick question on PO 0093847612"
    assert any(e["field"] == "subject" for e in rd["edits_applied"])


def test_missing_recipient_is_rejected_not_sent(client) -> None:
    rec = _seed()
    r = _draft(client, rec.id)  # no reply params → no recipient
    assert r.status_code == 200, r.text
    rd = exception_store.get(rec.id, "tenant-a").resolution_data["reply_draft"]
    assert rd["status"] == "REJECTED"
    assert "recipient" in rd["reason"].lower()
    assert rd["draft"] == {}


def test_draft_reply_on_non_intake_record_is_rejected(client) -> None:
    rec = _seed(intent="DUPLICATE_PO")
    r = _draft(client, rec.id, reply={"recipient": "x@y.com"},
               reason_tag="CONFIRMED_DUPLICATE")
    assert r.status_code in (409, 422)
