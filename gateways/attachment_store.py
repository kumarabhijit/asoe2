"""Attachment blob store — ASOE's production attachment store (DoR #10).

Inbound Customer-Inbox email attachments are persisted as content in ASOE's own
database (SQLite locally, Postgres in prod) and served from there, so retrieval
has **no outbound-network dependency** — there is nothing to SSRF on the read
path. (The `attachment_fetch` gateway's SSRF guard stays in place for the
separate case where an attachment is delivered as an external URL that must be
fetched once at ingestion before being stored here.)

Storage is pluggable behind a backend (mirroring `orchestration/outbox.py`): the
in-memory backend is the default (process-local); when `DATABASE_URL` is set the
DB backend persists to the V016 `email_attachment` table via
`db.repository.AttachmentRepository`. Content is raw bytes (BYTEA/BLOB, no
base64 inflation); a list read returns metadata only (content stripped) so it
never drags blobs — `get_attachment` is the single-blob fetch.

Integrity + safety:
  * a SHA-256 over the raw bytes is computed and stored at ingestion;
  * payloads larger than `policy.ATTACHMENT_MAX_BYTES` are rejected
    (`AttachmentTooLarge`) — an oversize/DoS guard;
  * every read is tenant-scoped.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urlparse
from uuid import uuid4

from contracts.policy import ATTACHMENT_MAX_BYTES

logger = logging.getLogger("asoe.gateways.attachment_store")


class AttachmentTooLarge(Exception):
    """Raised when an attachment exceeds `policy.ATTACHMENT_MAX_BYTES`."""


class AttachmentFetchError(Exception):
    """Raised by the store-backed fetcher when an attachment can't be served."""


@dataclass
class AttachmentRecord:
    id: str
    tenant_id: str
    name: str
    mime_type: str
    size_bytes: int
    sha256: str
    content: bytes
    created_at: str
    case_id: Optional[str] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class _InMemoryBackend:
    """Process-local store (default)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: Dict[tuple, AttachmentRecord] = {}

    def put(self, record: AttachmentRecord) -> None:
        with self._lock:
            self._records[(record.tenant_id, record.id)] = record

    def get(self, tenant_id: str, attachment_id: str) -> Optional[AttachmentRecord]:
        with self._lock:
            return self._records.get((tenant_id, attachment_id))

    def list_for_case(self, tenant_id: str, case_id: str) -> List[AttachmentRecord]:
        with self._lock:
            rows = [
                r for r in self._records.values()
                if r.tenant_id == tenant_id and r.case_id == case_id
            ]
        # Metadata view (content stripped) — parity with the DB backend, which
        # never SELECTs the blob on a list. Use get_attachment for the bytes.
        return [
            replace(r, content=b"")
            for r in sorted(rows, key=lambda r: r.created_at)
        ]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def delete(self, tenant_id: str, attachment_id: str) -> None:
        with self._lock:
            self._records.pop((tenant_id, attachment_id), None)


class _DbBackend:
    """Durable store backed by the `email_attachment` table (DATABASE_URL set)."""

    def __init__(self, repo) -> None:
        self._repo = repo

    @staticmethod
    def _to_record(row: Dict) -> AttachmentRecord:
        content = row.get("content")
        return AttachmentRecord(
            id=row["id"], tenant_id=row["tenant_id"], case_id=row.get("case_id"),
            name=row["name"], mime_type=row["mime_type"],
            size_bytes=int(row["size_bytes"]), sha256=row["sha256"],
            # `content` is absent on the metadata-only list projection.
            content=bytes(content) if content is not None else b"",
            created_at=row["created_at"],
        )

    def put(self, record: AttachmentRecord) -> None:
        self._repo.insert(record.tenant_id, {
            "id": record.id, "tenant_id": record.tenant_id, "case_id": record.case_id,
            "name": record.name, "mime_type": record.mime_type,
            "size_bytes": record.size_bytes, "sha256": record.sha256,
            "content": record.content,
            "created_at": record.created_at,
        })

    def get(self, tenant_id: str, attachment_id: str) -> Optional[AttachmentRecord]:
        row = self._repo.get(tenant_id, attachment_id)
        return self._to_record(row) if row is not None else None

    def list_for_case(self, tenant_id: str, case_id: str) -> List[AttachmentRecord]:
        return [self._to_record(r) for r in self._repo.list_for_case(tenant_id, case_id)]

    def clear(self) -> None:
        # Durable store — `clear` is a no-op for the DB backend (tests use a
        # fresh per-case adapter, so there is nothing to clear in-process).
        pass

    def delete(self, tenant_id: str, attachment_id: str) -> None:
        deleter = getattr(self._repo, "delete", None)
        if deleter is None:  # pragma: no cover - repo delete lands with the migration
            raise NotImplementedError(
                "AttachmentRepository.delete is required for erasure (ADR-044)"
            )
        deleter(tenant_id, attachment_id)


# ---------------------------------------------------------------------------
# Object-storage backend (ADR-044) — bytes in an object store (S3/GCS/MinIO),
# metadata in a DB-like index. Mirrors the other backends' interface so the GA
# migration is a config flip (the portability contract test locks this). The
# blob driver is pluggable: `for_testing()` uses an in-process driver so the
# contract runs without external infra; a real S3/MinIO driver implements the
# same `_BlobStore` methods.
# ---------------------------------------------------------------------------

class _BlobStore:
    """Minimal object-store driver surface."""

    def put_blob(self, key: str, data: bytes) -> None: ...
    def get_blob(self, key: str) -> Optional[bytes]: ...
    def delete_blob(self, key: str) -> None: ...
    def clear(self) -> None: ...


class _InMemoryBlobStore(_BlobStore):
    """Process-local blob driver — the `for_testing()` / dev driver."""

    def __init__(self) -> None:
        self._blobs: Dict[str, bytes] = {}

    def put_blob(self, key: str, data: bytes) -> None:
        self._blobs[key] = bytes(data)

    def get_blob(self, key: str) -> Optional[bytes]:
        return self._blobs.get(key)

    def delete_blob(self, key: str) -> None:
        self._blobs.pop(key, None)

    def clear(self) -> None:
        self._blobs.clear()


class ObjectStoreBackend:
    """Attachment backend keeping metadata in an index and bytes in an object
    store. `get` reconstitutes the full record (metadata + blob); `list` is
    metadata-only (never drags blobs) — parity with the DB backend, so preview's
    hot read path can leave the primary OLTP DB (ADR-044 §2.1)."""

    def __init__(self, blob_store: _BlobStore) -> None:
        self._blobs = blob_store
        self._meta: Dict[tuple, AttachmentRecord] = {}
        self._lock = threading.Lock()

    @classmethod
    def for_testing(cls) -> "ObjectStoreBackend":
        return cls(_InMemoryBlobStore())

    @staticmethod
    def _key(tenant_id: str, attachment_id: str) -> str:
        return f"{tenant_id}/{attachment_id}"

    def put(self, record: AttachmentRecord) -> None:
        with self._lock:
            self._blobs.put_blob(self._key(record.tenant_id, record.id), record.content)
            # Metadata index holds the record with content stripped.
            self._meta[(record.tenant_id, record.id)] = replace(record, content=b"")

    def get(self, tenant_id: str, attachment_id: str) -> Optional[AttachmentRecord]:
        with self._lock:
            meta = self._meta.get((tenant_id, attachment_id))
            if meta is None:
                return None
            data = self._blobs.get_blob(self._key(tenant_id, attachment_id)) or b""
            return replace(meta, content=data)

    def list_for_case(self, tenant_id: str, case_id: str) -> List[AttachmentRecord]:
        with self._lock:
            rows = [
                r for r in self._meta.values()
                if r.tenant_id == tenant_id and r.case_id == case_id
            ]
        return [replace(r, content=b"") for r in sorted(rows, key=lambda r: r.created_at)]

    def delete(self, tenant_id: str, attachment_id: str) -> None:
        with self._lock:
            self._meta.pop((tenant_id, attachment_id), None)
            self._blobs.delete_blob(self._key(tenant_id, attachment_id))

    def clear(self) -> None:
        with self._lock:
            self._meta.clear()
            self._blobs.clear()


def _select_backend():
    if os.getenv("DATABASE_URL", ""):
        try:
            from db.repository import AttachmentRepository
            from db.shared import get_shared_adapter
            return _DbBackend(AttachmentRepository(get_shared_adapter()))
        except Exception:  # pragma: no cover - fall back rather than crash boot
            logger.exception("attachment store DB backend init failed; using in-memory")
    return _InMemoryBackend()


_backend = _select_backend()


def configure_backend(backend) -> None:
    """Inject a backend (tests / explicit DB wiring)."""
    global _backend
    _backend = backend


def db_backend(repo) -> "_DbBackend":
    """Build a DB backend from an AttachmentRepository (for explicit wiring/tests)."""
    return _DbBackend(repo)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def store_attachment(
    tenant_id: str,
    name: str,
    mime_type: str,
    content: bytes,
    *,
    case_id: Optional[str] = None,
) -> AttachmentRecord:
    """Persist an attachment's bytes, returning the stored record.

    Computes the SHA-256 over the raw bytes and rejects payloads larger than
    `policy.ATTACHMENT_MAX_BYTES` (`AttachmentTooLarge`) before any write.
    """
    size = len(content)
    if size > ATTACHMENT_MAX_BYTES:
        raise AttachmentTooLarge(
            f"attachment {name!r} is {size} bytes; limit is {ATTACHMENT_MAX_BYTES}"
        )
    record = AttachmentRecord(
        id=str(uuid4()), tenant_id=tenant_id, case_id=case_id, name=name,
        mime_type=mime_type, size_bytes=size,
        sha256=hashlib.sha256(content).hexdigest(), content=content,
        created_at=_now(),
    )
    _backend.put(record)
    return record


def get_attachment(tenant_id: str, attachment_id: str) -> Optional[AttachmentRecord]:
    """Return a stored attachment for ``tenant_id``, or None if absent."""
    return _backend.get(tenant_id, attachment_id)


def list_case_attachments(tenant_id: str, case_id: str) -> List[AttachmentRecord]:
    """List a case's attachments (tenant-scoped, oldest first).

    Metadata view: the returned records carry empty ``content`` (the blob is
    not loaded for a list). Call `get_attachment` for the bytes.
    """
    return _backend.list_for_case(tenant_id, case_id)


# ---------------------------------------------------------------------------
# Right-to-erasure (ADR-044 §2.4)
# ---------------------------------------------------------------------------

_erasure_lock = threading.Lock()
_erasure_tombstones: Dict[tuple, Dict] = {}


def erase_attachment(backend, *, tenant_id: str, attachment_id: str) -> Optional[Dict]:
    """Erase a stored attachment's bytes + metadata and leave a PII-free
    tombstone (ADR-044 §2.4).

    The tombstone keeps the identity + content **hash** (so the audit trail can
    still prove a decision was made against content of hash X) but NEVER the
    content or the filename (which may itself be PII). Production routes the
    tombstone into the immutable audit chain (ADR-023); this module-level
    registry is the in-process analog the erasure contract runs against. Returns
    the tombstone, or None when there was nothing to erase.
    """
    record = backend.get(tenant_id, attachment_id)
    backend.delete(tenant_id, attachment_id)
    if record is None:
        return None
    tombstone: Dict = {
        "tenant_id": record.tenant_id,
        "attachment_id": record.id,
        "case_id": record.case_id,
        "sha256": record.sha256,
        "size_bytes": record.size_bytes,
        "mime_type": record.mime_type,
        "erased_at": _now(),
    }
    with _erasure_lock:
        _erasure_tombstones[(tenant_id, attachment_id)] = tombstone
    return tombstone


def get_erasure_tombstone(*, tenant_id: str, attachment_id: str) -> Optional[Dict]:
    """Return the erasure tombstone for an attachment, or None."""
    with _erasure_lock:
        return _erasure_tombstones.get((tenant_id, attachment_id))


def reset_erasure_tombstones() -> None:
    """Test helper — drop all tombstones."""
    with _erasure_lock:
        _erasure_tombstones.clear()


# ---------------------------------------------------------------------------
# Store-backed fetcher for AttachmentFetchGateway
# ---------------------------------------------------------------------------

def store_backed_fetcher(url: str, params: Dict) -> Dict:
    """Resolve an SSRF-validated attachment URL to its stored bytes.

    The attachment id is the last path segment of the manifest URL
    (``https://<allowlisted-host>/.../<id>``); the host is checked by the
    gateway's SSRF guard *before* this runs. Crucially, the owning **tenant is
    taken from the trusted request ``params``**, never from the URL — the
    manifest URL comes from an untrusted inbound email, so deriving the tenant
    from it would allow a crafted URL to read another tenant's attachment. The
    store read is tenant-scoped, so an id alone cannot cross tenants. No socket
    is opened (the bytes come from the internal store), so the URL host is a
    logical allowlist token, not a live fetch target. Raises
    `AttachmentFetchError` on a missing tenant or absent attachment — the
    gateway turns that into a FAILED response.
    """
    tenant_id = params.get("tenant_id")
    if not tenant_id:
        raise AttachmentFetchError("attachment fetch requires a trusted tenant_id param")
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts:
        raise AttachmentFetchError(f"malformed attachment url: {url!r}")
    attachment_id = parts[-1]
    record = get_attachment(tenant_id, attachment_id)
    if record is None:
        raise AttachmentFetchError(
            f"attachment not found: tenant={tenant_id} id={attachment_id}"
        )
    return {
        "url": url,
        "fetched": True,
        "content_type": record.mime_type,
        "bytes": record.size_bytes,
        "sha256": record.sha256,
        "content_b64": base64.b64encode(record.content).decode("ascii"),
    }
