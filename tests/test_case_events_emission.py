"""ADR-038 §H.6 / Phase 28.5 — `case_*` event emission tests.

Locks the three emission sites that drive the UI's `useCases` hook:

  * `case_resolver.materialise_for_event` emits `case_open` exactly
    once per materialisation (only when a new case is opened — not on
    every event that attaches to an existing case).
  * `POST /api/v1/cases/{id}/override` emits `case_update` on the
    status flip → `OPEN_AWAITING_HUMAN`.
  * `POST /api/v1/cases/{id}/override/cosign` emits `case_close` on
    approve (status → RESOLVED) and `case_update` on reject (status
    → OPEN_AGENT_PROCESSING).

The publisher is the in-memory `InMemoryPubSub` autoused by the
test app, so we assert by reading `pubsub.get_recent(tenant_id)`
and decoding the JSON envelope.
"""
from __future__ import annotations

import json
import os
import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.case_events import publish_case_close, publish_case_open, publish_case_update
from api.case_resolver import materialise_for_event
from api.deps import create_test_token
from api.pubsub import event_publisher
from api.store import case_store, exception_store
from contracts.models import OrderCase, OrderEvent


@pytest.fixture(autouse=True)
def _reset():
    case_store.clear()
    exception_store.clear() if hasattr(exception_store, "clear") else None
    if hasattr(event_publisher, "clear"):
        event_publisher.clear()
    yield
    case_store.clear()
    if hasattr(event_publisher, "clear"):
        event_publisher.clear()


@pytest.fixture()
def cosign_enabled(monkeypatch):
    monkeypatch.setenv("ASOE_CASE_COSIGN_ENABLED", "1")
    yield


@pytest.fixture()
def client():
    return TestClient(create_app(), raise_server_exceptions=True)


@pytest.fixture()
def manager_token():
    return create_test_token(
        sub="manager-A", roles=["manager"], org="tenant-a",
    )


@pytest.fixture()
def manager_b_token():
    return create_test_token(
        sub="manager-B", roles=["manager"], org="tenant-a",
    )


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _recent(tenant_id: str) -> list[dict]:
    return [json.loads(s) for s in event_publisher.get_recent(tenant_id)]


# ---------------------------------------------------------------------------
# `case_open` from case_resolver
# ---------------------------------------------------------------------------


class TestCaseOpenEmission:
    def test_manual_order_materialisation_emits_case_open(self):
        event = OrderEvent(
            order_id="PO-CO-1",
            event_type="EMAIL_ORDER_ENTRY_REQUEST",
            po_price=100.0,
            sap_base_price=120.0,
        )
        case = materialise_for_event("tenant-a", event, final_status=None)
        assert case is not None

        events = _recent("tenant-a")
        case_opens = [e for e in events if e["type"] == "case_open"]
        assert len(case_opens) == 1
        co = case_opens[0]
        assert co["case_id"] == case.case_id
        assert co["tenant_id"] == "tenant-a"
        assert co["payload"]["source"] == "manual_order"
        assert co["payload"]["customer_po_number"] == "PO-CO-1"

    def test_second_event_attaching_to_same_case_does_not_re_emit(self):
        # Two events sharing the same customer_po → same case.
        event_a = OrderEvent(
            order_id="PO-CO-2",
            event_type="EMAIL_ORDER_ENTRY_REQUEST",
            po_price=100.0, sap_base_price=120.0,
        )
        event_b = OrderEvent(
            order_id="PO-CO-2",
            event_type="EMAIL_ORDER_ENTRY_REQUEST",
            po_price=110.0, sap_base_price=120.0,
        )
        case_a = materialise_for_event("tenant-a", event_a, final_status=None)
        case_b = materialise_for_event("tenant-a", event_b, final_status=None)
        assert case_a is not None and case_b is not None
        assert case_a.case_id == case_b.case_id  # same case

        events = _recent("tenant-a")
        case_opens = [e for e in events if e["type"] == "case_open"]
        # Exactly ONE — the second materialise should NOT re-emit.
        assert len(case_opens) == 1

    def test_automated_clean_terminal_does_not_emit(self):
        event = OrderEvent(
            order_id="PO-CO-3",
            event_type="EDI_X12_850",
            po_price=100.0, sap_base_price=100.0,
        )
        # Clean COMPLETE on automated → no case opens.
        case = materialise_for_event(
            "tenant-a", event, final_status="COMPLETE",
        )
        assert case is None
        assert _recent("tenant-a") == []


# ---------------------------------------------------------------------------
# `case_update` from /cases/{id}/override
# ---------------------------------------------------------------------------


class TestCaseUpdateEmission:
    def test_override_initiation_emits_case_update_with_status_flip(
        self, client, manager_token, cosign_enabled,
    ):
        # Materialise a case for the cosign endpoint to target.
        event = OrderEvent(
            order_id="PO-CU-1",
            event_type="EMAIL_ORDER_ENTRY_REQUEST",
            po_price=100.0, sap_base_price=120.0,
        )
        case = materialise_for_event("tenant-a", event, final_status=None)
        assert case is not None

        # Clear the case_open event so we assert only the update below.
        event_publisher.clear()

        r = client.post(
            f"/api/v1/cases/{case.case_id}/override",
            json={
                "pending_action": "ALLOW_BOTH",
                "pending_reason_tag": "OTHER",
                "aggregate_financial_impact_usd": 5000.0,
                "child_exception_ids": [],
                "notes": "Manager A initiating cosign",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 200, r.text

        events = _recent("tenant-a")
        updates = [e for e in events if e["type"] == "case_update"]
        assert len(updates) == 1
        u = updates[0]
        assert u["case_id"] == case.case_id
        assert u["payload"]["status"] == "OPEN_AWAITING_HUMAN"
        assert "status" in u["payload"]["updated_fields"]
        assert "pending_override" in u["payload"]["updated_fields"]


# ---------------------------------------------------------------------------
# `case_close` / `case_update` from cosign resolve
# ---------------------------------------------------------------------------


class TestCaseCloseEmission:
    def _stage_pending_override(self, client, case_id, manager_token):
        r = client.post(
            f"/api/v1/cases/{case_id}/override",
            json={
                "pending_action": "ALLOW_BOTH",
                "pending_reason_tag": "OTHER",
                "aggregate_financial_impact_usd": 5000.0,
                "child_exception_ids": [],
                "notes": "Manager A initiating cosign",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 200, r.text

    def test_cosign_approve_emits_case_close_terminal(
        self, client, manager_token, manager_b_token, cosign_enabled,
    ):
        event = OrderEvent(
            order_id="PO-CC-1",
            event_type="EMAIL_ORDER_ENTRY_REQUEST",
            po_price=100.0, sap_base_price=120.0,
        )
        case = materialise_for_event("tenant-a", event, final_status=None)
        assert case is not None
        self._stage_pending_override(client, case.case_id, manager_token)
        event_publisher.clear()

        r = client.post(
            f"/api/v1/cases/{case.case_id}/override/cosign",
            json={"approve": True, "notes": "Manager B approving cosign"},
            headers=_auth(manager_b_token),
        )
        assert r.status_code == 200, r.text

        events = _recent("tenant-a")
        closes = [e for e in events if e["type"] == "case_close"]
        assert len(closes) == 1
        close = closes[0]
        assert close["case_id"] == case.case_id
        assert close["payload"]["status"] == "RESOLVED"
        assert close["payload"]["closed_at"]  # non-empty timestamp

    def test_cosign_reject_emits_case_update_not_close(
        self, client, manager_token, manager_b_token, cosign_enabled,
    ):
        event = OrderEvent(
            order_id="PO-CC-2",
            event_type="EMAIL_ORDER_ENTRY_REQUEST",
            po_price=100.0, sap_base_price=120.0,
        )
        case = materialise_for_event("tenant-a", event, final_status=None)
        assert case is not None
        self._stage_pending_override(client, case.case_id, manager_token)
        event_publisher.clear()

        r = client.post(
            f"/api/v1/cases/{case.case_id}/override/cosign",
            json={"approve": False, "notes": "Manager B rejecting cosign"},
            headers=_auth(manager_b_token),
        )
        assert r.status_code == 200, r.text

        events = _recent("tenant-a")
        # Reject is NOT terminal — emit `case_update`, not `case_close`.
        assert [e for e in events if e["type"] == "case_close"] == []
        updates = [e for e in events if e["type"] == "case_update"]
        assert len(updates) == 1
        assert updates[0]["payload"]["status"] == "OPEN_AGENT_PROCESSING"
