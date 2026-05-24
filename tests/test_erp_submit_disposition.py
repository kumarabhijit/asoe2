"""ADR-042 Phase 3 — operator-triggered ERP submit via /disposition.

The financially-binding order-entry submit is an explicit SUBMIT_TO_ERP
disposition that re-enters the graph (SubmitToErpRecipe → `erp` gateway write).
Sub-$10k submits run immediately; >=$10k stage PENDING_COSIGN and run the write
only on four-eyes cosign-approve (materiality from the SAP re-price, DoR #3).
Operator corrections ride as disposition params and are audited before/after.

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
    "source_type": "PDF", "confidence": 0.94,
    "header": {"customer_po": "0093847612", "order_type": "ZOR",
               "sales_org": "1000", "dist_channel": "10",
               "requested_date": "2025-03-17"},
    "customer_name": "Walmart Stores Inc", "customer_bp": "300001",
    "line_items": [{"line_num": "001", "material": "BEV-COLA-12PK",
                    "quantity": 480, "uom": "CS", "unit_price": 8.64}],
    "validation_flags": [],
}


@pytest.fixture()
def client():
    exception_store.clear()
    _clear_idempotency_cache()
    return TestClient(create_app(), raise_server_exceptions=False)


def _auth(roles, sub):
    return {"Authorization": f"Bearer {create_test_token(roles=roles, org='tenant-a', sub=sub)}"}


def _seed(order_value_usd: float, *, intent: str = "MANUAL_ORDER_INTAKE",
          line_items=None):
    extraction = dict(_EXTRACTION)
    if line_items is not None:
        extraction = {**extraction, "line_items": line_items}
    return exception_store.create(
        tenant_id="tenant-a", order_id="EML-PO-1", event_type="MANUAL_ORDER_INTAKE",
        trace_id="tr-1", intent=intent, shadow_verdict="YELLOW",
        final_status="MANUAL_REVIEW_REQUIRED",
        resolution_data={"recommended_action": "ONE_CLICK_APPROVE"},
        original_event={"order_id": "EML-PO-1", "event_type": "MANUAL_ORDER_INTAKE",
                        "po_price": 0.0, "sap_base_price": 0.0, "retailer_id": "R-1"},
        enrichment_context={
            "order_entry_extraction": extraction,
            "sap_data": {"system": "S4H_PRD", "validation_status": "ok",
                         "order_value_usd": order_value_usd},
        },
    )


def _disposition(client, rec_id, token_hdr, **body):
    payload = {"action": "SUBMIT_TO_ERP", "reason_tag": "OTHER", "notes": "ship it"}
    payload.update(body)
    return client.patch(f"/api/v1/exceptions/{rec_id}/disposition",
                        json=payload, headers=token_hdr)


def test_sub_threshold_submit_runs_and_resolves(client) -> None:
    rec = _seed(5_000.0)
    r = _disposition(client, rec.id, _auth(["analyst"], "user-A"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lifecycle_state"] == "RESOLVED"
    stored = exception_store.get(rec.id, "tenant-a")
    sub = stored.resolution_data["erp_submission"]
    assert sub["status"] == "SUCCESS"
    assert sub["sap_doc_number"]  # the erp gateway returned a doc number
    assert sub["erp_payload"]["customer_po"] == "0093847612"


def test_submit_applies_and_audits_operator_corrections(client) -> None:
    rec = _seed(5_000.0)
    r = _disposition(client, rec.id, _auth(["analyst"], "user-A"),
                     corrections={"lines": {"001": {"quantity": 500}}})
    assert r.status_code == 200, r.text
    stored = exception_store.get(rec.id, "tenant-a")
    sub = stored.resolution_data["erp_submission"]
    assert sub["erp_payload"]["line_items"][0]["quantity"] == 500
    assert {"line_num": "001", "field": "quantity", "before": 480, "after": 500} \
        in sub["corrections_applied"]


def test_high_value_submit_stages_cosign_then_runs_on_approve(client) -> None:
    rec = _seed(45_200.0)
    r = _disposition(client, rec.id, _auth(["manager"], "user-A"))
    assert r.status_code == 200, r.text
    assert r.json()["lifecycle_state"] == "PENDING_COSIGN"
    # No ERP write yet — the submit is parked for four-eyes.
    staged = exception_store.get(rec.id, "tenant-a")
    assert "erp_submission" not in (staged.resolution_data or {})

    # A different manager cosigns → the submit runs.
    r2 = client.post(
        f"/api/v1/exceptions/{rec.id}/override/cosign",
        json={"approve": True, "notes": "Reviewed; authorise the submit."},
        headers=_auth(["manager"], "user-B"),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["lifecycle_state"] == "RESOLVED"
    stored = exception_store.get(rec.id, "tenant-a")
    assert stored.resolution_data["erp_submission"]["status"] == "SUCCESS"
    assert stored.resolution_data["erp_submission"]["sap_doc_number"]


def test_submit_on_non_intake_record_is_rejected(client) -> None:
    rec = _seed(5_000.0, intent="DUPLICATE_PO")
    r = _disposition(client, rec.id, _auth(["analyst"], "user-A"),
                     reason_tag="CONFIRMED_DUPLICATE")
    assert r.status_code in (409, 422)


def test_rejected_order_does_not_write_to_erp(client) -> None:
    # No line items → the recipe REJECTS → no ERP write, surfaced reason.
    rec = _seed(5_000.0, line_items=[])
    r = _disposition(client, rec.id, _auth(["analyst"], "user-A"))
    assert r.status_code == 200, r.text
    stored = exception_store.get(rec.id, "tenant-a")
    sub = stored.resolution_data["erp_submission"]
    assert sub["status"] == "REJECTED"
    assert sub["sap_doc_number"] is None
    # not resolved — stays in review for correction.
    assert stored.lifecycle_state != "RESOLVED"
