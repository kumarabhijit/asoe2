"""DoR gate #5 — delivery idempotency on the buyer-reply send path.

The HTTP Idempotency-Key dedupes a client retry of one request; this gate proves
*delivery-level* dedup: the same reply (recipient + content) for a case reaches
the buyer-notification gateway AT MOST ONCE, even across two distinct SEND_REPLY
requests without idempotency keys. A genuinely different reply still sends.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.routes.exceptions import _clear_idempotency_cache, _clear_delivery_dedup
from api.store import exception_store
from gateways.registry import get_gateway

_EXTRACTION = {
    "source_type": "PDF", "confidence": 0.78,
    "header": {"customer_po": "0093847612", "order_type": "ZOR"},
    "customer_name": "Walmart Stores Inc", "customer_bp": "300001",
    "line_items": [{"line_num": "001", "material": "BEV-COLA-12PK",
                    "quantity": 480, "uom": "CS", "unit_price": 8.64}],
    "validation_flags": [],
}


@pytest.fixture()
def client():
    exception_store.clear()
    _clear_idempotency_cache()
    _clear_delivery_dedup()
    return TestClient(create_app(), raise_server_exceptions=False)


def _auth(sub="user-A"):
    return {"Authorization": f"Bearer {create_test_token(roles=['analyst'], org='tenant-a', sub=sub)}"}


def _seed():
    return exception_store.create(
        tenant_id="tenant-a", order_id="EML-PO-1", event_type="MANUAL_ORDER_INTAKE",
        trace_id="tr-1", intent="MANUAL_ORDER_INTAKE", shadow_verdict="YELLOW",
        final_status="MANUAL_REVIEW_REQUIRED",
        resolution_data={"recommended_action": "REQUEST_CLARIFICATION"},
        original_event={"order_id": "EML-PO-1", "event_type": "MANUAL_ORDER_INTAKE",
                        "po_price": 0.0, "sap_base_price": 0.0, "retailer_id": "R-1"},
        enrichment_context={"order_entry_extraction": _EXTRACTION},
    )


def _send(client, rec_id, recipient):
    return client.patch(
        f"/api/v1/exceptions/{rec_id}/disposition",
        json={"action": "SEND_REPLY", "reason_tag": "OTHER",
              "notes": "sending reply", "reply": {"recipient": recipient}},
        headers=_auth(),
    )


def _send_calls() -> int:
    gw = get_gateway("buyer_notification")
    return sum(1 for c in gw.calls if c.operation == "send")


def test_duplicate_send_does_not_deliver_twice(client):
    rec = _seed()
    baseline = _send_calls()

    r1 = _send(client, rec.id, "orders@walmart.example")
    assert r1.status_code == 200, r1.text
    assert exception_store.get(rec.id, "tenant-a").resolution_data["reply_sent"]["status"] == "SENT"
    after_first = _send_calls()
    assert after_first == baseline + 1  # delivered once

    # Same reply again — idempotent: no second delivery, still reports SENT.
    r2 = _send(client, rec.id, "orders@walmart.example")
    assert r2.status_code == 200, r2.text
    assert r2.json()["resolution_data"]["reply_sent"]["status"] == "SENT"
    assert _send_calls() == after_first  # the gateway was NOT hit again


def test_different_reply_still_delivers(client):
    rec = _seed()
    _send(client, rec.id, "orders@walmart.example")
    after_first = _send_calls()

    # A genuinely different recipient is a different delivery → sends again.
    r = _send(client, rec.id, "buyer@kroger.example")
    assert r.status_code == 200, r.text
    assert _send_calls() == after_first + 1


def test_delivery_key_is_stamped_on_the_sent_record(client):
    rec = _seed()
    _send(client, rec.id, "orders@walmart.example")
    rs = exception_store.get(rec.id, "tenant-a").resolution_data["reply_sent"]
    assert rs["status"] == "SENT"
    assert isinstance(rs.get("delivery_key"), str) and len(rs["delivery_key"]) == 64
