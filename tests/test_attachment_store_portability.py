"""CP-B RED gate (ADR-044 §2.1) — storage backend portability contract.

Test-first (`xfail(strict=True)`; removed at CP-E). The panel's GA precondition:
get blobs out of the primary OLTP DB onto object storage. That migration must be
a backend swap, not a rewrite — so the object-store backend must satisfy exactly
the same observable semantics as the proven in-memory backend
(`put`/`get` tenant-isolated/`list_for_case` content-stripped/`clear`, sha256
integrity). Landing this lock in Phase 1 makes the GA swap a config flip; CP-E
generalises it into a parametrised parity sweep over both backends.
"""

from __future__ import annotations

from gateways.attachment_store import AttachmentRecord, _InMemoryBackend


def _rec(i: str = "att-1") -> AttachmentRecord:
    return AttachmentRecord(
        id=i, tenant_id="t1", case_id="case-1", name="po.pdf",
        mime_type="application/pdf", size_bytes=3, sha256="a" * 64,
        content=b"PDF", created_at="2026-05-25T00:00:00Z",
    )


def test_object_store_backend_matches_in_memory_semantics():
    # Import first: until the object-store backend exists this records as xfail.
    from gateways.attachment_store import ObjectStoreBackend

    for backend in (_InMemoryBackend(), ObjectStoreBackend.for_testing()):
        backend.put(_rec("att-1"))
        got = backend.get("t1", "att-1")
        assert got is not None and got.content == b"PDF" and got.sha256 == "a" * 64
        assert backend.get("t2", "att-1") is None            # tenant isolation
        listed = backend.list_for_case("t1", "case-1")
        assert len(listed) == 1 and listed[0].content == b""  # metadata-only list
        backend.clear()
        assert backend.get("t1", "att-1") is None
