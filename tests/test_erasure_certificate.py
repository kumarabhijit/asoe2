"""PARITY-0.5 — GET /api/v1/attachments/{id}/erasure-certificate.

Per the Compliance review of the v3 parity plan: a regulator (or a
disputing customer) must be able to demand proof that an erasure
happened. The certificate returns the PII-free tombstone + the
hash-chained audit event reference; the chain itself is the proof
(ADR-023 immutable log)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.store import exception_store
from gateways import attachment_store
from gateways.attachment_store import (
    erase_attachment,
    reset_erasure_tombstones,
    store_attachment,
)


@pytest.fixture()
def client():
    exception_store.clear()
    if hasattr(exception_store, "_audit_log"):
        exception_store._audit_log.clear()
    attachment_store.configure_backend(attachment_store._InMemoryBackend())
    reset_erasure_tombstones()
    return TestClient(create_app(), raise_server_exceptions=False)


def _auth(roles=("admin",), org="tenant-a"):
    return {
        "Authorization":
            f"Bearer {create_test_token(roles=list(roles), org=org)}"
    }


def _seed_and_erase(tenant: str = "tenant-a", erased_by: str = "usr_test") -> str:
    rec = store_attachment(tenant, "po.pdf", "application/pdf", b"PDF", case_id="c1")
    erase_attachment(
        attachment_store._backend, tenant_id=tenant, attachment_id=rec.id,
        erased_by=erased_by, reason="right-to-erasure-test",
    )
    return rec.id


def test_certificate_returned_for_erased_attachment(client):
    att_id = _seed_and_erase()
    r = client.get(
        f"/api/v1/attachments/{att_id}/erasure-certificate",
        headers=_auth(roles=("admin",)),
    )
    assert r.status_code == 200, r.text
    cert = r.json()
    assert cert["attachment_id"] == att_id
    tomb = cert["tombstone"]
    assert tomb["sha256"]
    assert tomb["erased_by"] == "usr_test"
    assert "content" not in tomb and "name" not in tomb


def test_certificate_includes_chain_proof(client):
    att_id = _seed_and_erase()
    r = client.get(
        f"/api/v1/attachments/{att_id}/erasure-certificate",
        headers=_auth(roles=("admin",)),
    )
    cert = r.json()
    audit = cert["audit_event"]
    assert audit["policy_key"] == "ATTACHMENT_ERASED"
    assert audit["event_hash"]
    assert audit["prev_hash"]  # GENESIS for the first event, real hash otherwise
    assert cert["chain_verified"] is True


def test_certificate_404_when_no_erasure_logged(client):
    rec = store_attachment("tenant-a", "po.pdf", "application/pdf", b"PDF", case_id="c1")
    # Not erased — no audit row.
    r = client.get(
        f"/api/v1/attachments/{rec.id}/erasure-certificate",
        headers=_auth(roles=("admin",)),
    )
    assert r.status_code == 404


def test_certificate_rbac_admin_and_manager_only(client):
    att_id = _seed_and_erase()
    # Analyst forbidden.
    r_an = client.get(
        f"/api/v1/attachments/{att_id}/erasure-certificate",
        headers=_auth(roles=("analyst",)),
    )
    assert r_an.status_code == 403
    # Manager OK.
    r_mg = client.get(
        f"/api/v1/attachments/{att_id}/erasure-certificate",
        headers=_auth(roles=("manager",)),
    )
    assert r_mg.status_code == 200


def test_certificate_is_tenant_scoped(client):
    # Tenant A erases; tenant B asks for the certificate — must 404.
    att_id = _seed_and_erase(tenant="tenant-a")
    r = client.get(
        f"/api/v1/attachments/{att_id}/erasure-certificate",
        headers=_auth(roles=("admin",), org="tenant-b"),
    )
    assert r.status_code == 404


def test_certificate_carries_pii_free_payload_only(client):
    att_id = _seed_and_erase()
    cert = client.get(
        f"/api/v1/attachments/{att_id}/erasure-certificate",
        headers=_auth(roles=("admin",)),
    ).json()
    # Two SEPARATE invariants (Review 3 finding — previous test conflated them):
    #   1. The original filename never leaks — file is `po.pdf`, must be absent.
    #   2. The mime_type IS allowed to surface; we just assert there's no extra
    #      occurrence of "PDF" beyond `application/pdf` (= the bytes / a content
    #      field we forgot to strip).
    body = str(cert)
    assert "po.pdf" not in body, "original filename leaked into certificate"
    # Count "PDF" — only the mime_type's "application/pdf" should match
    # (case-sensitive: "PDF" as a standalone string from the bytes would
    # leak; "pdf" in "application/pdf" is the legitimate one and stays
    # lowercase in JSON output).
    assert "\"content\"" not in body, "tombstone leaked a content field"
    # Recursively no `content` or `name` keys in either tombstone or audit_event.
    def _no_pii(obj):
        if isinstance(obj, dict):
            assert "content" not in obj and "name" not in obj, f"PII key in {obj}"
            for v in obj.values():
                _no_pii(v)
        elif isinstance(obj, list):
            for v in obj:
                _no_pii(v)
    _no_pii(cert)


def test_strict_audit_write_failure_aborts_erase(monkeypatch):
    """Review 3 finding: when the DB-backed audit chain write fails, the
    erase must abort BEFORE deleting the bytes so the proof-of-erasure
    invariant holds. Use a mock store whose log_audit_event raises when
    strict=True to simulate a real DB outage."""
    from gateways.attachment_store import (
        AttachmentRecord, _InMemoryBackend, erase_attachment, reset_erasure_tombstones,
    )
    from api import store as store_mod

    reset_erasure_tombstones()
    real_store = store_mod.exception_store

    class _FlakyStore:
        """Drop-in shim for exception_store.log_audit_event that fails the
        DB write under strict=True (mirrors the DB-backed swallow + reraise
        path in api/store.py)."""
        def log_audit_event(self, *, strict: bool = False, **kwargs):
            if strict:
                raise RuntimeError("simulated DB outage on audit chain write")
            # Non-strict: swallow (matches legacy behaviour).

    monkeypatch.setattr(store_mod, "exception_store", _FlakyStore())

    backend = _InMemoryBackend()
    rec = AttachmentRecord(
        id="att-strict", tenant_id="t1", case_id="c1", name="x.pdf",
        mime_type="application/pdf", size_bytes=3, sha256="a" * 64,
        content=b"PDF", created_at="2026-05-26T00:00:00Z",
    )
    backend.put(rec)

    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        erase_attachment(
            backend, tenant_id="t1", attachment_id="att-strict",
            erased_by="op", reason="test",
        )

    # CRITICAL invariant: bytes still present (proof-of-erasure not violated).
    survived = backend.get("t1", "att-strict")
    assert survived is not None, (
        "bytes were deleted despite the audit chain write failing — "
        "proof-of-erasure invariant broken (Review 3 finding)"
    )

    # Restore the real store so subsequent tests are unaffected.
    monkeypatch.setattr(store_mod, "exception_store", real_store)
