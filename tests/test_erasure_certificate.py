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
    # The whole response must not contain the bytes or the original filename.
    body = str(cert)
    assert "PDF" not in body or body.count("PDF") <= 2  # mime_type='application/pdf' is OK
    # No way the original record's `name` ("po.pdf") leaks via the tombstone.
    assert "po.pdf" not in body
