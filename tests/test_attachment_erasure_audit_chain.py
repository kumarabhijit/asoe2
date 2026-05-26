"""PARITY-0.5 — erase_attachment routes the tombstone into the immutable
audit chain (ADR-023), so the first real erasure can be proved to a regulator.

Per the Compliance review of the v3 parity plan, this gate must land
**before** any real-tenant data lands. The in-process registry
(`_erasure_tombstones`) stays as a fast lookup; the source of truth is
the hash-chained `policy_audit_log` (ADR-023) that
`exception_store.log_audit_event` writes to.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api.store import exception_store
from gateways.attachment_store import (
    AttachmentRecord,
    _InMemoryBackend,
    erase_attachment,
    reset_erasure_tombstones,
)


@pytest.fixture(autouse=True)
def _clean():
    exception_store.clear()
    if hasattr(exception_store, "_audit_log"):
        exception_store._audit_log.clear()
    reset_erasure_tombstones()
    yield
    exception_store.clear()
    reset_erasure_tombstones()


def _rec(id_: str = "att-1", sha: str = "a" * 64) -> AttachmentRecord:
    return AttachmentRecord(
        id=id_, tenant_id="t1", case_id="case-1", name="po.pdf",
        mime_type="application/pdf", size_bytes=3, sha256=sha,
        content=b"PDF", created_at="2026-05-26T00:00:00Z",
    )


def test_erase_writes_attachment_erased_event_to_audit_chain():
    backend = _InMemoryBackend()
    backend.put(_rec())

    erase_attachment(
        backend, tenant_id="t1", attachment_id="att-1",
        erased_by="usr_marcus", reason="customer-erasure-request",
    )

    log = exception_store.get_audit_log("t1")
    erasure_events = [e for e in log if e["policy_key"] == "ATTACHMENT_ERASED"]
    assert len(erasure_events) == 1
    e = erasure_events[0]
    assert e["changed_by"] == "usr_marcus"
    assert e["change_reason"] == "customer-erasure-request"


def test_event_carries_tombstone_in_new_value():
    backend = _InMemoryBackend()
    backend.put(_rec())

    erase_attachment(backend, tenant_id="t1", attachment_id="att-1", erased_by="op")

    e = next(
        x for x in exception_store.get_audit_log("t1")
        if x["policy_key"] == "ATTACHMENT_ERASED"
    )
    tomb = e["new_value"]
    # PII-free: never the bytes, never the filename.
    assert "content" not in tomb
    assert "name" not in tomb
    # Identity + content hash are preserved.
    assert tomb["attachment_id"] == "att-1"
    assert tomb["sha256"] == "a" * 64
    assert tomb["mime_type"] == "application/pdf"
    assert tomb["size_bytes"] == 3
    # erased_at is an ISO-8601 UTC timestamp.
    parsed = datetime.fromisoformat(tomb["erased_at"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_audit_chain_verifies_after_erasure():
    backend = _InMemoryBackend()
    backend.put(_rec("att-1"))
    backend.put(_rec("att-2", sha="b" * 64))

    erase_attachment(backend, tenant_id="t1", attachment_id="att-1", erased_by="op")
    erase_attachment(backend, tenant_id="t1", attachment_id="att-2", erased_by="op")

    ok, break_idx = exception_store.verify_audit_chain("t1")
    assert ok is True
    assert break_idx is None


def test_erase_of_missing_attachment_writes_no_audit_event():
    # Idempotent / no-op: erasing a non-existent attachment must NOT
    # generate a spurious audit event (would pollute the chain).
    backend = _InMemoryBackend()
    result = erase_attachment(
        backend, tenant_id="t1", attachment_id="never-existed", erased_by="op",
    )
    assert result is None
    log = exception_store.get_audit_log("t1")
    assert not any(e["policy_key"] == "ATTACHMENT_ERASED" for e in log)


def test_erased_by_defaults_to_system_when_unspecified():
    # Background sweeper / system jobs may erase without a user identity.
    # Default to "system:retention-sweeper" so the audit row never has a
    # blank `changed_by`.
    backend = _InMemoryBackend()
    backend.put(_rec())

    erase_attachment(backend, tenant_id="t1", attachment_id="att-1")

    e = next(
        x for x in exception_store.get_audit_log("t1")
        if x["policy_key"] == "ATTACHMENT_ERASED"
    )
    assert e["changed_by"].startswith("system")


def test_tombstone_lookup_still_works_after_chain_write():
    # Backwards-compat: the in-process registry (fast lookup) must keep
    # working so existing get_erasure_tombstone callers don't need an
    # audit-log query.
    from gateways.attachment_store import get_erasure_tombstone

    backend = _InMemoryBackend()
    backend.put(_rec())
    erase_attachment(backend, tenant_id="t1", attachment_id="att-1", erased_by="op")

    tomb = get_erasure_tombstone(tenant_id="t1", attachment_id="att-1")
    assert tomb is not None
    assert tomb["sha256"] == "a" * 64
