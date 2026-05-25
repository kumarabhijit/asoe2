"""Attachment download endpoint (DoR #10) — tenant + RBAC gated.

GET /api/v1/cases/{case_id}/attachments/{attachment_id} streams a stored
attachment's bytes to an authorised operator. The attachment must belong to the
caller's tenant AND the path case_id, else 404. Forced download
(Content-Disposition: attachment) so file content can never render inline.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from gateways import attachment_store
from gateways.attachment_store import store_attachment


@pytest.fixture(autouse=True)
def _mem_backend():
    attachment_store.configure_backend(attachment_store._InMemoryBackend())
    yield
    attachment_store.configure_backend(attachment_store._InMemoryBackend())


@pytest.fixture()
def client():
    return TestClient(create_app(), raise_server_exceptions=False)


def _auth(roles=("analyst",), org="tenant-a"):
    return {"Authorization": f"Bearer {create_test_token(roles=list(roles), org=org)}"}


def test_download_returns_the_stored_bytes(client):
    rec = store_attachment("tenant-a", "po.pdf", "application/pdf", b"PDFBYTES", case_id="case-1")
    r = client.get(f"/api/v1/cases/case-1/attachments/{rec.id}", headers=_auth())
    assert r.status_code == 200
    assert r.content == b"PDFBYTES"
    assert r.headers["content-type"].startswith("application/pdf")
    cd = r.headers["content-disposition"]
    assert "attachment" in cd and "po.pdf" in cd


def test_cross_tenant_download_is_404(client):
    rec = store_attachment("tenant-a", "po.pdf", "application/pdf", b"secret", case_id="case-1")
    r = client.get(f"/api/v1/cases/case-1/attachments/{rec.id}", headers=_auth(org="tenant-b"))
    assert r.status_code == 404


def test_wrong_case_id_is_404(client):
    rec = store_attachment("tenant-a", "po.pdf", "application/pdf", b"x", case_id="case-1")
    r = client.get(f"/api/v1/cases/OTHER/attachments/{rec.id}", headers=_auth())
    assert r.status_code == 404


def test_missing_attachment_is_404(client):
    r = client.get("/api/v1/cases/case-1/attachments/does-not-exist", headers=_auth())
    assert r.status_code == 404


def test_download_requires_auth(client):
    rec = store_attachment("tenant-a", "po.pdf", "application/pdf", b"x", case_id="case-1")
    r = client.get(f"/api/v1/cases/case-1/attachments/{rec.id}")
    assert r.status_code in (401, 403)


class TestSandboxIngestToDownloadEndToEnd:
    """The store is exercised end-to-end through real routes: the sandbox
    ingest producer writes, the download endpoint reads (DoR #10 gap close)."""

    def test_ingest_then_download_roundtrips(self, client):
        import base64
        blob = b"END-TO-END-BYTES"
        post = client.post(
            "/api/v1/_sandbox/cases/case-9/attachments",
            json={
                "name": "po.pdf", "mime_type": "application/pdf",
                "content_b64": base64.b64encode(blob).decode("ascii"),
            },
            headers=_auth(roles=("manager",)),
        )
        assert post.status_code == 200, post.text
        aid = post.json()["attachment_id"]
        assert post.json()["sha256"]

        got = client.get(f"/api/v1/cases/case-9/attachments/{aid}", headers=_auth())
        assert got.status_code == 200
        assert got.content == blob

    def test_ingest_default_sample_is_downloadable(self, client):
        post = client.post(
            "/api/v1/_sandbox/cases/case-10/attachments",
            json={},
            headers=_auth(roles=("analyst",)),
        )
        assert post.status_code == 200, post.text
        aid = post.json()["attachment_id"]
        got = client.get(f"/api/v1/cases/case-10/attachments/{aid}", headers=_auth())
        assert got.status_code == 200
        assert got.content.startswith(b"%PDF")

    def test_ingested_attachment_is_tenant_isolated(self, client):
        post = client.post(
            "/api/v1/_sandbox/cases/case-11/attachments",
            json={}, headers=_auth(roles=("manager",), org="tenant-a"),
        )
        aid = post.json()["attachment_id"]
        # tenant-b cannot download tenant-a's ingested attachment.
        got = client.get(
            f"/api/v1/cases/case-11/attachments/{aid}", headers=_auth(org="tenant-b"),
        )
        assert got.status_code == 404


def test_filename_header_is_sanitised(client):
    # A crafted filename must not inject CRLF / quotes into the header.
    rec = store_attachment(
        "tenant-a", 'evil"\r\nSet-Cookie: x.pdf', "application/pdf", b"x", case_id="case-1",
    )
    r = client.get(f"/api/v1/cases/case-1/attachments/{rec.id}", headers=_auth())
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert "\r" not in cd and "\n" not in cd
    assert "Set-Cookie" not in r.headers
