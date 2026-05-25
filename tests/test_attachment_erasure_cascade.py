"""CP-B RED gate (ADR-044 §2.4) — right-to-erasure cascade vs audit preservation.

Test-first (`xfail(strict=True)`; removed at CP-E with compliance sign-off).
Erasing a customer document removes the bytes (and, later, frozen renditions +
derived anchors) — but the immutable audit chain (ADR-023) is NOT mutated: it
retains a tombstone (attachment_id + sha256 + erasure event), never the content.
This resolves the erase-vs-audit conflict: the proof a decision was made against
content of hash X survives; the PII does not.
"""

from __future__ import annotations

import pytest

from gateways.attachment_store import AttachmentRecord, _InMemoryBackend

pytestmark = pytest.mark.xfail(
    reason="ADR-044 erase_attachment + tombstone land at CP-E",
    strict=True,
)


def _rec() -> AttachmentRecord:
    return AttachmentRecord(
        id="att-1", tenant_id="t1", case_id="case-1", name="po.pdf",
        mime_type="application/pdf", size_bytes=3, sha256="a" * 64,
        content=b"PDF", created_at="2026-05-25T00:00:00Z",
    )


def test_erasure_removes_bytes_but_leaves_a_tombstone():
    from gateways.attachment_store import erase_attachment, get_erasure_tombstone

    backend = _InMemoryBackend()
    backend.put(_rec())

    erase_attachment(backend, tenant_id="t1", attachment_id="att-1")

    # Bytes are gone.
    assert backend.get("t1", "att-1") is None
    # The audit-preserving tombstone remains: identity + content hash, no PII.
    tomb = get_erasure_tombstone(tenant_id="t1", attachment_id="att-1")
    assert tomb is not None
    assert tomb["sha256"] == "a" * 64
    assert "content" not in tomb and "name" not in tomb
