"""Phase 3 #3 — hash-chained audit log tamper-evidence tests.

Each audit event carries event_hash = sha256(prev_hash || canonical_json).
Editing or deleting an event in the middle of the chain causes every
subsequent hash check to fail. verify_audit_chain() returns the first
break index for fast diagnostics.
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
    # Reset the module-level audit log too.
    if hasattr(exception_store, "_audit_log"):
        exception_store._audit_log.clear()
    return TestClient(create_app(), raise_server_exceptions=False)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_three_events(client) -> None:
    """Produce three audit events: one escalate + two disposition attempts."""
    analyst = create_test_token(sub="a@x", roles=["analyst"], org="tenant-a")
    manager = create_test_token(sub="m@x", roles=["manager"], org="tenant-a")
    # Create a pending-review exception
    r = client.post(
        "/api/v1/exceptions/resolve/explain",
        json={
            "order_id": "PO-1",
            "po_price": 100.0,
            "sap_base_price": 120.0,
            "event_type": "EDI_850_PRICE_MISMATCH",
        },
        headers=_auth(analyst),
    )
    eid = r.json()["exception_id"]
    # Event 1: escalate
    client.post(
        f"/api/v1/exceptions/{eid}/escalate",
        json={"reason": "first escalate"},
        headers=_auth(analyst),
    )
    # Event 2: another escalate on a different record (still tenant-a)
    r = client.post(
        "/api/v1/exceptions/resolve/explain",
        json={
            "order_id": "PO-2",
            "po_price": 50.0,
            "sap_base_price": 65.0,
            "event_type": "EDI_850_PRICE_MISMATCH",
        },
        headers=_auth(analyst),
    )
    eid2 = r.json()["exception_id"]
    client.post(
        f"/api/v1/exceptions/{eid2}/escalate",
        json={"reason": "second escalate"},
        headers=_auth(analyst),
    )
    # Event 3: disposition on eid1 after it was escalated
    client.patch(
        f"/api/v1/exceptions/{eid}/disposition",
        json={"action": "ALLOW_BOTH", "notes": "manager resolves", "reason_tag": "other"},
        headers=_auth(manager),
    )


def test_chain_valid_after_writes(client):
    _seed_three_events(client)
    events = exception_store.get_audit_log("tenant-a")
    assert len(events) >= 3
    valid, break_at = exception_store.verify_audit_chain("tenant-a")
    assert valid is True, f"chain broken at index {break_at}"
    assert break_at is None


def test_genesis_prev_hash_for_first_event(client):
    _seed_three_events(client)
    events = exception_store.get_audit_log("tenant-a")
    assert events[0]["prev_hash"] == "GENESIS"
    assert events[0]["event_hash"] is not None


def test_chain_link_prev_hash_equals_predecessor(client):
    _seed_three_events(client)
    events = exception_store.get_audit_log("tenant-a")
    for i in range(1, len(events)):
        assert events[i]["prev_hash"] == events[i - 1]["event_hash"], (
            f"chain broken at index {i}"
        )


def test_tamper_detection_on_edit(client):
    _seed_three_events(client)
    events = exception_store.get_audit_log("tenant-a")
    assert len(events) >= 3
    # Simulate a tampered audit row — edit the middle event's changed_by
    # field. Rebuilding the hash from the mutated payload should no longer
    # match the stored event_hash.
    events[1]["changed_by"] = "attacker@x"
    valid, break_at = exception_store.verify_audit_chain("tenant-a")
    assert valid is False
    assert break_at == 1


def test_tamper_detection_on_delete(client):
    _seed_three_events(client)
    # Remove the middle event from the underlying list. Subsequent events'
    # prev_hash no longer links to an existing event → verify fails.
    exception_store._audit_log = [
        e for i, e in enumerate(exception_store._audit_log) if i != 1
    ]
    valid, break_at = exception_store.verify_audit_chain("tenant-a")
    assert valid is False
    # The break point is where the removed event used to be.
    assert break_at is not None


def test_chains_are_per_tenant(client):
    """A separate tenant's chain starts from its own GENESIS and is
    independent of tenant-a's chain."""
    _seed_three_events(client)
    # Now write an event for tenant-b.
    tb_manager = create_test_token(sub="mb@x", roles=["manager"], org="tenant-b")
    r = client.post(
        "/api/v1/exceptions/resolve/explain",
        json={
            "order_id": "PO-B",
            "po_price": 99.0,
            "sap_base_price": 120.0,
            "event_type": "EDI_850_PRICE_MISMATCH",
        },
        headers=_auth(tb_manager),
    )
    eid_b = r.json()["exception_id"]
    client.post(
        f"/api/v1/exceptions/{eid_b}/escalate",
        json={"reason": "tenant-b escalate"},
        headers=_auth(tb_manager),
    )
    assert exception_store.verify_audit_chain("tenant-a") == (True, None)
    assert exception_store.verify_audit_chain("tenant-b") == (True, None)
    # Tenant-b's first event must carry GENESIS, not one of tenant-a's hashes.
    tb_events = exception_store.get_audit_log("tenant-b")
    assert tb_events[0]["prev_hash"] == "GENESIS"
