"""DoR #6 — DB-backed effect outbox (durable ledger across restarts).

Exercises the V015 effect_outbox migration + OutboxRepository + the
orchestration.outbox DB backend against a real (in-memory SQLite) adapter. The
in-memory backend is the default; this proves the DB path has parity: record →
list_pending → reconcile (retry/escalate) → survives a fresh repo instance on
the same adapter (durability).
"""

from __future__ import annotations

import pytest

from contracts.models import GatewayResponse
from db.connection import SQLiteAdapter
from db.repository import OutboxRepository
from orchestration import outbox


@pytest.fixture()
def adapter():
    a = SQLiteAdapter(":memory:")
    a.apply_schema()
    return a


@pytest.fixture()
def db_outbox(adapter):
    """Point the outbox module at a DB backend on a fresh adapter."""
    outbox.configure_backend(outbox.db_backend(OutboxRepository(adapter)))
    yield adapter
    outbox.configure_backend(outbox._InMemoryBackend())  # restore default


class _Scripted:
    def __init__(self, statuses):
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


def test_migration_creates_effect_outbox_table(adapter):
    with adapter.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='effect_outbox'")
        assert cur.fetchone() is not None


def test_record_and_pending_persist(db_outbox):
    outbox.record_effect(tenant_id="t", gateway="erp", operation="create_sales_order", status="SUCCESS")
    outbox.record_effect(tenant_id="t", gateway="buyer_notification", operation="send", status="FAILED", params={"recipient": "x@y"})
    pending = outbox.pending_compensation("t")
    assert [p.gateway for p in pending] == ["buyer_notification"]
    assert len(outbox.all_entries("t")) == 2
    # The committed SUCCESS is not in the queue.
    assert all(p.committed is False for p in pending)


def test_durability_across_repo_instances(db_outbox):
    outbox.record_effect(tenant_id="t", gateway="g", operation="o", status="FAILED")
    # A brand-new repo on the SAME adapter still sees the row (it's persisted).
    fresh = OutboxRepository(db_outbox)
    assert len(fresh.list_pending("t")) == 1


def test_reconcile_against_db_retries_and_escalates(db_outbox):
    outbox.record_effect(tenant_id="t", gateway="buyer_notification", operation="send", status="FAILED", params={"recipient": "x@y"})
    # First pass: gateway still failing → attempt 1, stays pending.
    r1 = outbox.reconcile_pending(tenant_id="t", executor=_Scripted({"buyer_notification": ["FAILED"]}), max_attempts=2)
    assert r1 == {"retried": 1, "compensated": 0, "escalated": 0, "still_pending": 1}
    # Second pass at max_attempts=2 → escalated, leaves the queue.
    r2 = outbox.reconcile_pending(tenant_id="t", executor=_Scripted({"buyer_notification": ["FAILED"]}), max_attempts=2)
    assert r2["escalated"] == 1
    assert outbox.pending_compensation("t") == []
    escalated = [e for e in outbox.all_entries("t") if e.escalated]
    assert len(escalated) == 1 and escalated[0].attempts == 2


def test_reconcile_against_db_compensates_on_success(db_outbox):
    outbox.record_effect(tenant_id="t", gateway="buyer_notification", operation="send", status="FAILED", params={"recipient": "x@y"})
    report = outbox.reconcile_pending(tenant_id="t", executor=_Scripted({"buyer_notification": ["SUCCESS"]}))
    assert report["compensated"] == 1
    assert outbox.pending_compensation("t") == []
