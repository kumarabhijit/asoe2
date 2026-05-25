"""DB-backed attachment blob store — the production attachment store (DoR #10).

Attachments are persisted as content in ASOE's own database (SQLite locally,
Postgres in prod) rather than fetched from an external service, so retrieval has
no outbound-network dependency. The store is tenant-scoped, computes a SHA-256
over the raw bytes for integrity, and rejects oversize payloads at ingestion.

Exercises both backends: the in-memory default and the V016 email_attachment
migration + AttachmentRepository against a real (in-memory SQLite) adapter, to
prove the DB path has parity (put → get → tenant isolation → durability).
"""

from __future__ import annotations

import hashlib

import pytest

from contracts.policy import ATTACHMENT_MAX_BYTES
from db.connection import SQLiteAdapter
from db.repository import AttachmentRepository
from gateways import attachment_store
from gateways.attachment_store import (
    AttachmentTooLarge,
    get_attachment,
    list_case_attachments,
    store_attachment,
)


@pytest.fixture(autouse=True)
def _mem_backend():
    attachment_store.configure_backend(attachment_store._InMemoryBackend())
    yield
    attachment_store.configure_backend(attachment_store._InMemoryBackend())


# ---------------------------------------------------------------------------
# Store API (backend-agnostic — runs against the in-memory default)
# ---------------------------------------------------------------------------

def test_put_then_get_roundtrips_content_and_metadata():
    rec = store_attachment("t1", "po.pdf", "application/pdf", b"hello", case_id="c1")
    got = get_attachment("t1", rec.id)
    assert got is not None
    assert got.content == b"hello"
    assert got.name == "po.pdf"
    assert got.mime_type == "application/pdf"
    assert got.size_bytes == 5
    assert got.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert got.case_id == "c1"


def test_get_is_tenant_scoped():
    rec = store_attachment("t1", "a.pdf", "application/pdf", b"x")
    # A different tenant must never read another tenant's attachment.
    assert get_attachment("t2", rec.id) is None


def test_oversize_is_rejected_at_ingestion():
    big = b"x" * (ATTACHMENT_MAX_BYTES + 1)
    with pytest.raises(AttachmentTooLarge):
        store_attachment("t1", "big.bin", "application/octet-stream", big)


def test_at_limit_is_accepted():
    rec = store_attachment("t1", "ok.bin", "application/octet-stream", b"x" * ATTACHMENT_MAX_BYTES)
    assert rec.size_bytes == ATTACHMENT_MAX_BYTES


def test_get_missing_returns_none():
    assert get_attachment("t1", "does-not-exist") is None


def test_list_case_attachments_is_case_and_tenant_scoped():
    a = store_attachment("t1", "a.pdf", "application/pdf", b"a", case_id="c1")
    b = store_attachment("t1", "b.pdf", "application/pdf", b"b", case_id="c1")
    store_attachment("t1", "c.pdf", "application/pdf", b"c", case_id="c2")
    store_attachment("t2", "d.pdf", "application/pdf", b"d", case_id="c1")
    ids = {r.id for r in list_case_attachments("t1", "c1")}
    assert ids == {a.id, b.id}


def test_list_returns_metadata_only_get_returns_content():
    # DB-bloat guard: a list read never carries the blob; get does.
    rec = store_attachment("t1", "a.pdf", "application/pdf", b"BLOBBYTES", case_id="c1")
    listed = list_case_attachments("t1", "c1")
    assert len(listed) == 1
    assert listed[0].size_bytes == 9          # metadata preserved
    assert listed[0].sha256 == rec.sha256
    assert listed[0].content == b""           # blob NOT loaded on list
    assert get_attachment("t1", rec.id).content == b"BLOBBYTES"


# ---------------------------------------------------------------------------
# DB backend parity (V016 migration + AttachmentRepository on SQLite)
# ---------------------------------------------------------------------------

@pytest.fixture()
def adapter():
    a = SQLiteAdapter(":memory:")
    a.apply_schema()
    return a


def test_migration_creates_email_attachment_table(adapter):
    with adapter.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='email_attachment'"
        )
        assert cur.fetchone() is not None


def test_db_backend_roundtrips_and_survives_fresh_repo(adapter):
    attachment_store.configure_backend(
        attachment_store.db_backend(AttachmentRepository(adapter))
    )
    try:
        rec = store_attachment("t1", "po.pdf", "application/pdf", b"binary\x00bytes", case_id="c1")
        # A fresh repo instance on the same adapter still reads it (durability).
        attachment_store.configure_backend(
            attachment_store.db_backend(AttachmentRepository(adapter))
        )
        got = get_attachment("t1", rec.id)
        assert got is not None
        assert got.content == b"binary\x00bytes"
        assert got.sha256 == hashlib.sha256(b"binary\x00bytes").hexdigest()
        assert get_attachment("t2", rec.id) is None
    finally:
        attachment_store.configure_backend(attachment_store._InMemoryBackend())
