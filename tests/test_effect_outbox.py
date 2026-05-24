"""DoR gate #6 — effect outbox + compensation queue.

`apply_effects` fires gateway side effects after the recipe result commits. The
outbox records every effect outcome durably: a SUCCESS external write is
committed (ERP-submit-OK durability); a failure is queued for compensation
(reply-fail), surfaced via pending_compensation() until mark_compensated()
clears it. Unit tests lock the ledger; an integration drive proves apply_effects
populates it through a real SEND_REPLY (success) and a forced gateway failure.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.routes.exceptions import _clear_idempotency_cache
from api.store import exception_store
from contracts.models import GatewayRequest, GatewayResponse
from gateways.registry import register_gateway
from orchestration import outbox


# --- unit: the ledger + compensation queue -------------------------------

@pytest.fixture(autouse=True)
def _reset_outbox():
    outbox.reset()
    yield
    outbox.reset()


def test_success_effect_is_committed_not_pending():
    outbox.record_effect(
        tenant_id="t", gateway="erp", operation="create_sales_order",
        status="SUCCESS", recipe="SubmitToErpRecipe.py", recipe_status="SUCCESS",
    )
    assert outbox.pending_compensation("t") == []
    entries = outbox.all_entries("t")
    assert len(entries) == 1 and entries[0].committed is True


def test_failed_effect_needs_compensation_and_can_be_cleared():
    e = outbox.record_effect(
        tenant_id="t", gateway="buyer_notification", operation="send",
        status="FAILED", recipe="ReplyDraftRecipe.py",
        recipe_status="READY_TO_SEND", error="smtp timeout",
    )
    pending = outbox.pending_compensation("t")
    assert [p.id for p in pending] == [e.id]
    assert outbox.mark_compensated(e.id) is True
    assert outbox.pending_compensation("t") == []


def test_pending_is_tenant_scoped():
    outbox.record_effect(tenant_id="t1", gateway="g", operation="o", status="FAILED")
    outbox.record_effect(tenant_id="t2", gateway="g", operation="o", status="FAILED")
    assert len(outbox.pending_compensation("t1")) == 1
    assert len(outbox.pending_compensation()) == 2


# --- the reconciliation worker (DoR #6) ----------------------------------

class _ScriptedExecutor:
    """Stand-in GatewayExecutor returning scripted statuses per gateway."""

    def __init__(self, statuses: dict[str, list[str]]):
        self._statuses = statuses
        self.runs = 0

    def run(self, request):
        self.runs += 1
        seq = self._statuses.get(request.gateway_name, ["FAILED"])
        status = seq[min(len(seq) - 1, self.runs - 1)]
        return GatewayResponse(
            gateway_name=request.gateway_name, operation=request.operation,
            status=status,
        )


def test_reconcile_retries_and_clears_on_success():
    outbox.record_effect(
        tenant_id="t", gateway="buyer_notification", operation="send",
        status="FAILED", params={"recipient": "x@y.example"},
    )
    ex = _ScriptedExecutor({"buyer_notification": ["SUCCESS"]})
    report = outbox.reconcile_pending(tenant_id="t", executor=ex)
    assert report["retried"] == 1 and report["compensated"] == 1
    assert outbox.pending_compensation("t") == []  # cleared


def test_reconcile_escalates_after_max_attempts():
    outbox.record_effect(
        tenant_id="t", gateway="erp", operation="create_sales_order",
        status="FAILED", params={"order_id": "SO-1"},
    )
    ex = _ScriptedExecutor({"erp": ["FAILED"]})
    # 3 passes at max_attempts=3 → escalated, then it drops out of the queue.
    for _ in range(2):
        outbox.reconcile_pending(tenant_id="t", executor=ex, max_attempts=3)
        assert outbox.pending_compensation("t"), "still pending before exhaustion"
    final = outbox.reconcile_pending(tenant_id="t", executor=ex, max_attempts=3)
    assert final["escalated"] == 1
    assert outbox.pending_compensation("t") == []
    escalated = [e for e in outbox.all_entries("t") if e.escalated]
    assert len(escalated) == 1 and escalated[0].attempts == 3


# --- integration: apply_effects populates the outbox ---------------------

_EXTRACTION = {
    "source_type": "PDF", "confidence": 0.78,
    "header": {"customer_po": "0093847612"},
    "customer_name": "Walmart Stores Inc", "customer_bp": "300001",
    "line_items": [{"line_num": "001", "material": "BEV-COLA-12PK",
                    "quantity": 480, "uom": "CS", "unit_price": 8.64}],
    "validation_flags": [],
}


class _FailingSend:
    name = "buyer_notification"

    def execute(self, request: GatewayRequest) -> GatewayResponse:
        return GatewayResponse(
            gateway_name="buyer_notification", operation=request.operation,
            status="FAILED", error="smtp unavailable",
        )

    def health_check(self) -> bool:
        return True


@pytest.fixture()
def client():
    exception_store.clear()
    _clear_idempotency_cache()
    outbox.reset()
    return TestClient(create_app(), raise_server_exceptions=False)


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


def _send(client, rec_id, recipient="orders@walmart.example"):
    return client.patch(
        f"/api/v1/exceptions/{rec_id}/disposition",
        json={"action": "SEND_REPLY", "reason_tag": "OTHER", "notes": "send",
              "reply": {"recipient": recipient}},
        headers={"Authorization": f"Bearer {create_test_token(roles=['analyst'], org='tenant-a')}"},
    )


def test_successful_send_records_a_committed_outbox_entry(client):
    rec = _seed()
    assert _send(client, rec.id).status_code == 200
    entries = [e for e in outbox.all_entries("tenant-a") if e.gateway == "buyer_notification"]
    assert entries, "expected a buyer_notification outbox entry"
    assert entries[-1].committed is True
    assert outbox.pending_compensation("tenant-a") == []


def test_failed_send_queues_compensation(client):
    register_gateway(_FailingSend())  # override the success stub for this case
    rec = _seed()
    assert _send(client, rec.id).status_code == 200  # disposition succeeds; send failed
    pending = outbox.pending_compensation("tenant-a")
    assert any(p.gateway == "buyer_notification" and p.operation == "send" for p in pending)
    # And the persisted reply_sent reflects the failure (not masked as success).
    assert exception_store.get(rec.id, "tenant-a").resolution_data["reply_sent"]["status"] == "FAILED"


def test_reconcile_endpoint_is_admin_only(client):
    outbox.record_effect(tenant_id="tenant-a", gateway="g", operation="o", status="FAILED")
    analyst = {"Authorization": f"Bearer {create_test_token(roles=['analyst'], org='tenant-a')}"}
    assert client.post("/api/v1/outbox/reconcile", headers=analyst).status_code in (401, 403)
    admin = {"Authorization": f"Bearer {create_test_token(roles=['admin'], org='tenant-a')}"}
    res = client.post("/api/v1/outbox/reconcile", headers=admin)
    assert res.status_code == 200, res.text
    assert set(res.json()) == {"retried", "compensated", "escalated", "still_pending"}
