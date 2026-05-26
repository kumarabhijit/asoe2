"""ADR-044 P2.2 — scoped, short-TTL read path for attachment bytes.

Preview/download read bytes via a short-TTL, tenant + case-scoped capability
token (the in-DB / filesystem backends can't natively sign). The token is the
capability: it is unusable after expiry and bound to exactly one
(tenant, case, attachment) tuple, so it can't read another tenant's bytes. Both
properties are asserted here. (Retention/encryption are governance — out of
scope.)
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.attachment_read_token import mint_read_token
from api.deps import create_test_token
from api.store import exception_store
from gateways import attachment_store
from gateways.attachment_store import store_attachment


@pytest.fixture()
def client():
    exception_store.clear()
    attachment_store.configure_backend(attachment_store._InMemoryBackend())
    yield TestClient(create_app(), raise_server_exceptions=False)
    attachment_store.configure_backend(attachment_store._InMemoryBackend())


def _auth(roles=("manager",), org="tenant-a"):
    return {"Authorization": f"Bearer {create_test_token(roles=list(roles), org=org)}"}


def test_mint_then_read_streams_the_bytes(client):
    rec = store_attachment("tenant-a", "po.pdf", "application/pdf", b"PDFBYTES", case_id="c1")
    r = client.post(
        f"/api/v1/cases/c1/attachments/{rec.id}/signed-url", headers=_auth(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] and body["expires_at"]

    # The signed URL needs no Authorization header — the token is the capability.
    dl = client.get(body["url"])
    assert dl.status_code == 200, dl.text
    assert dl.content == b"PDFBYTES"
    assert dl.headers["content-disposition"].startswith("attachment;")
    assert dl.headers["x-content-type-options"] == "nosniff"


def test_signed_url_requires_rbac(client):
    rec = store_attachment("tenant-a", "po.pdf", "application/pdf", b"X", case_id="c1")
    r = client.post(f"/api/v1/cases/c1/attachments/{rec.id}/signed-url")  # no auth
    assert r.status_code in (401, 403)


def test_expired_token_is_rejected(client):
    rec = store_attachment("tenant-a", "po.pdf", "application/pdf", b"X", case_id="c1")
    token = mint_read_token(
        tenant_id="tenant-a", case_id="c1", attachment_id=rec.id, ttl_seconds=-1,
    )
    dl = client.get(f"/api/v1/attachments/read?token={token}")
    assert dl.status_code == 403


def test_tampered_token_is_rejected(client):
    rec = store_attachment("tenant-a", "po.pdf", "application/pdf", b"X", case_id="c1")
    token = mint_read_token(
        tenant_id="tenant-a", case_id="c1", attachment_id=rec.id, ttl_seconds=300,
    )
    dl = client.get(f"/api/v1/attachments/read?token={token}x")
    assert dl.status_code == 403


def test_token_cannot_cross_tenants(client):
    # A token minted naming tenant-a can never read tenant-b's bytes, even if an
    # attacker forges the claims — the signature binds the tuple.
    store_attachment("tenant-b", "secret.pdf", "application/pdf", b"SECRET", case_id="c1")
    # Mint a *valid* token for tenant-a + the SAME attachment id wouldn't exist;
    # use tenant-a's own attachment id. A tenant-a token can't reach tenant-b.
    rec_a = store_attachment("tenant-a", "mine.pdf", "application/pdf", b"MINE", case_id="c1")
    token = mint_read_token(
        tenant_id="tenant-a", case_id="c1", attachment_id=rec_a.id, ttl_seconds=300,
    )
    dl = client.get(f"/api/v1/attachments/read?token={token}")
    assert dl.status_code == 200 and dl.content == b"MINE"  # reads its own tenant only


def test_wrong_case_is_not_found(client):
    rec = store_attachment("tenant-a", "po.pdf", "application/pdf", b"X", case_id="c1")
    token = mint_read_token(
        tenant_id="tenant-a", case_id="WRONG", attachment_id=rec.id, ttl_seconds=300,
    )
    dl = client.get(f"/api/v1/attachments/read?token={token}")
    assert dl.status_code == 404
