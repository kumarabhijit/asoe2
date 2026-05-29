"""ADR-042 Phase 4 — DRAFT_REPLY disposition (compose a buyer reply).

An explicit DRAFT_REPLY disposition re-enters the graph (Shadow → ReplyDraftRecipe)
to compose a buyer reply draft from the reviewed extraction + operator reply
params. The draft is persisted to resolution_data.reply_draft; the record stays
in review (a draft is not a resolution). No email is sent — the send is a
separate authorised action.

Written test-first.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.pubsub import event_publisher
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
    if hasattr(event_publisher, "clear"):
        event_publisher.clear()
    return TestClient(create_app(), raise_server_exceptions=False)


def _events(tenant_id="tenant-a"):
    return [json.loads(e) for e in event_publisher.get_recent(tenant_id)]


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


def test_first_draft_seeds_v1_ai_generated_revision(client) -> None:
    rec = _seed()
    _draft(client, rec.id, reply={"recipient": "orders@walmart.example"})
    rd = exception_store.get(rec.id, "tenant-a").resolution_data["reply_draft"]
    assert len(rd["revisions"]) == 1
    rev = rd["revisions"][0]
    assert rev["version"] == 1
    assert rev["source"] == "AI_GENERATED"
    assert rev["author"] == "ai:ReplyDraftRecipe.py"
    assert rev["edits_applied"] == []  # nothing to diff for the seed
    assert rev["subject"] == rd["draft"]["subject"]


def test_operator_edit_appends_v2_revision_with_before_after(client) -> None:
    rec = _seed()
    # v1 — AI-generated draft.
    _draft(client, rec.id, reply={"recipient": "orders@walmart.example"})
    v1 = exception_store.get(rec.id, "tenant-a").resolution_data["reply_draft"]
    v1_subject = v1["draft"]["subject"]
    # v2 — operator edits the subject.
    _draft(client, rec.id, reply={
        "recipient": "orders@walmart.example",
        "edits": {"subject": "Quick question on PO 0093847612"},
    })
    rd = exception_store.get(rec.id, "tenant-a").resolution_data["reply_draft"]
    assert [r["version"] for r in rd["revisions"]] == [1, 2]
    v2 = rd["revisions"][1]
    assert v2["source"] == "OPERATOR_EDIT"
    assert v2["author"] == "user-A"  # operator sub, not the AI service id
    assert v2["subject"] == "Quick question on PO 0093847612"
    # before/after computed vs the PREVIOUS revision (v1), not the template.
    edit = next(e for e in v2["edits_applied"] if e["field"] == "subject")
    assert edit["before"] == v1_subject
    assert edit["after"] == "Quick question on PO 0093847612"
    # Top-level mirrors the latest revision so existing readers keep working.
    assert rd["draft"]["subject"] == "Quick question on PO 0093847612"
    assert any(e["field"] == "subject" for e in rd["edits_applied"])


def test_operator_edit_emits_reply_edited_audit_event(client) -> None:
    rec = _seed()
    _draft(client, rec.id, reply={"recipient": "orders@walmart.example"})
    _draft(client, rec.id, reply={
        "recipient": "orders@walmart.example",
        "edits": {"subject": "Quick question on PO 0093847612"},
    })
    keys = [e["policy_key"] for e in exception_store.get_audit_log("tenant-a")]
    assert "EXCEPTION_REPLY_DRAFTED" in keys   # v1 generation
    assert "EXCEPTION_REPLY_EDITED" in keys     # v2 operator edit
    edited = [e for e in exception_store.get_audit_log("tenant-a")
              if e["policy_key"] == "EXCEPTION_REPLY_EDITED"]
    assert edited[-1]["new_value"]["revision_version"] == 2
    assert edited[-1]["new_value"]["revision_source"] == "OPERATOR_EDIT"
    assert edited[-1]["changed_by"] == "user-A"


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


def _send(client, rec_id, **body):
    payload = {"action": "SEND_REPLY", "reason_tag": "OTHER", "notes": "sending reply"}
    payload.update(body)
    return client.patch(f"/api/v1/exceptions/{rec_id}/disposition",
                        json=payload, headers=_auth())


def test_send_reply_fires_buyer_notification_and_records_sent(client) -> None:
    rec = _seed()
    r = _send(client, rec.id, reply={"recipient": "orders@walmart.example"})
    assert r.status_code == 200, r.text
    rs = exception_store.get(rec.id, "tenant-a").resolution_data["reply_sent"]
    assert rs["status"] == "SENT"
    assert rs["delivered"] is True
    assert rs["gateway_status"] == "SUCCESS"
    assert rs["recipient"] == "orders@walmart.example"
    assert rs["sent_by"] == "user-A"


def test_send_reply_does_not_resolve_the_record(client) -> None:
    rec = _seed()
    before = exception_store.get(rec.id, "tenant-a").lifecycle_state
    _send(client, rec.id, reply={"recipient": "orders@walmart.example"})
    after = exception_store.get(rec.id, "tenant-a").lifecycle_state
    assert after == before  # the case stays open awaiting the buyer
    assert after != "RESOLVED"


def test_send_without_recipient_does_not_send(client) -> None:
    rec = _seed()
    r = _send(client, rec.id)  # no recipient
    assert r.status_code == 200, r.text
    rs = exception_store.get(rec.id, "tenant-a").resolution_data["reply_sent"]
    assert rs["status"] == "FAILED"
    assert rs["delivered"] is False
    assert "recipient" in (rs["reason"] or "").lower()


def test_send_reply_on_non_intake_record_is_rejected(client) -> None:
    rec = _seed(intent="DUPLICATE_PO")
    r = _send(client, rec.id, reply={"recipient": "x@y.com"},
              reason_tag="CONFIRMED_DUPLICATE")
    assert r.status_code in (409, 422)


def test_draft_reply_emits_reply_drafted_event(client) -> None:
    rec = _seed()
    _draft(client, rec.id, reply={"recipient": "orders@walmart.example"})
    drafted = [e for e in _events() if e["type"] == "reply_drafted"]
    assert len(drafted) == 1
    ev = drafted[0]
    assert ev["exception_id"] == rec.id
    assert ev["payload"]["status"] == "DRAFTED"
    assert ev["payload"]["recipient"] == "orders@walmart.example"


def test_send_reply_emits_reply_sent_event(client) -> None:
    rec = _seed()
    _send(client, rec.id, reply={"recipient": "orders@walmart.example"})
    sent = [e for e in _events() if e["type"] == "reply_sent"]
    assert len(sent) == 1
    assert sent[0]["payload"]["status"] == "SENT"
    assert sent[0]["payload"]["delivered"] is True
