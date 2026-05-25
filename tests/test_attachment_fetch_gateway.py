"""DoR #10 — the SSRF guard wired into the attachment_fetch gateway.

Every attachment URL passes `hardening.ssrf.validate_outbound_url` BEFORE any
retrieval: allowlisted HTTPS hosts fetch (stub blob in sandbox), while
off-allowlist / internal / metadata URLs are refused with status FAILED and the
fetcher is never invoked. Verified directly and through the GatewayExecutor.
"""

from __future__ import annotations

import pytest

from contracts.models import GatewayRequest
from gateways.attachment_fetch import AttachmentFetchGateway
from gateways.executor import GatewayExecutor
from gateways.registry import clear_registry, register_gateway

_ALLOW = frozenset({"attachments.acme.example"})


@pytest.fixture(autouse=True)
def _clean():
    clear_registry()
    yield
    clear_registry()


def _req(url, **extra):
    return GatewayRequest(
        gateway_name="attachment_fetch", operation="fetch",
        params={"url": url, **extra}, trace_id="t-1", timeout_ms=2000,
    )


def test_allowlisted_url_fetches():
    gw = AttachmentFetchGateway(allowed_hosts=_ALLOW)
    resp = gw.execute(_req("https://attachments.acme.example/po/42.pdf"))
    assert resp.status == "SUCCESS"
    assert resp.data["fetched"] is True


@pytest.mark.parametrize("url", [
    "https://evil.example/x.pdf",                       # off-allowlist
    "http://attachments.acme.example/x.pdf",            # non-https
    "https://169.254.169.254/latest/meta-data/",        # cloud metadata
    "https://127.0.0.1/x.pdf",                          # loopback
])
def test_unsafe_url_is_blocked_before_fetch(url):
    calls = []
    gw = AttachmentFetchGateway(
        allowed_hosts=_ALLOW | {"169.254.169.254", "127.0.0.1"},
        fetcher=lambda u, p: calls.append(u) or {"fetched": True},
    )
    resp = gw.execute(_req(url, resolve=False))
    assert resp.status == "FAILED"
    assert "SSRF blocked" in (resp.error or "")
    assert calls == []  # the fetcher was never reached


def test_missing_url_fails_cleanly():
    gw = AttachmentFetchGateway(allowed_hosts=_ALLOW)
    resp = gw.execute(GatewayRequest(
        gateway_name="attachment_fetch", operation="fetch",
        params={}, trace_id="t", timeout_ms=1000,
    ))
    assert resp.status == "FAILED" and "url" in (resp.error or "")


def test_through_executor_blocks_internal_url():
    register_gateway(AttachmentFetchGateway(allowed_hosts=_ALLOW | {"10.0.0.5"}))
    resp = GatewayExecutor().run(_req("https://10.0.0.5/secret", resolve=False))
    assert resp.status == "FAILED"
    assert "SSRF blocked" in (resp.error or "")


def test_fetcher_failure_becomes_failed_not_a_crash():
    def _boom(_u, _p):
        raise RuntimeError("upstream 500")
    gw = AttachmentFetchGateway(allowed_hosts=_ALLOW, fetcher=_boom)
    resp = gw.execute(_req("https://attachments.acme.example/po/42.pdf"))
    assert resp.status == "FAILED"
    assert "attachment fetch failed" in (resp.error or "")


# ---------------------------------------------------------------------------
# Store-backed fetcher — serves real bytes from the DB attachment store, still
# behind the SSRF host allowlist (DoR #10 live path).
# ---------------------------------------------------------------------------

class TestStoreBackedFetcher:
    def setup_method(self):
        from gateways import attachment_store
        attachment_store.configure_backend(attachment_store._InMemoryBackend())

    def teardown_method(self):
        from gateways import attachment_store
        attachment_store.configure_backend(attachment_store._InMemoryBackend())

    def _gw(self):
        from gateways.attachment_store import store_backed_fetcher
        return AttachmentFetchGateway(allowed_hosts=_ALLOW, fetcher=store_backed_fetcher)

    def test_allowlisted_url_serves_stored_bytes(self):
        from gateways.attachment_store import store_attachment
        rec = store_attachment("acme", "po.pdf", "application/pdf", b"PDFDATA", case_id="c1")
        url = f"https://attachments.acme.example/{rec.id}"
        resp = self._gw().execute(_req(url, tenant_id="acme"))
        assert resp.status == "SUCCESS"
        assert resp.data["bytes"] == 7
        assert resp.data["content_type"] == "application/pdf"
        import base64
        assert base64.b64decode(resp.data["content_b64"]) == b"PDFDATA"

    def test_missing_attachment_is_failed(self):
        url = "https://attachments.acme.example/does-not-exist"
        resp = self._gw().execute(_req(url, tenant_id="acme"))
        assert resp.status == "FAILED"
        assert "attachment fetch failed" in (resp.error or "")

    def test_tenant_comes_from_trusted_params_not_the_url(self):
        # An attacker-crafted manifest URL cannot read another tenant's
        # attachment: the store lookup uses the trusted params tenant_id.
        from gateways.attachment_store import store_attachment
        rec = store_attachment("victim", "secret.pdf", "application/pdf", b"SECRET")
        # URL path even names the victim tenant — it is ignored.
        url = f"https://attachments.acme.example/victim/{rec.id}"
        resp = self._gw().execute(_req(url, tenant_id="attacker"))
        assert resp.status == "FAILED"
        assert "attachment fetch failed" in (resp.error or "")

    def test_missing_tenant_param_is_failed(self):
        from gateways.attachment_store import store_attachment
        rec = store_attachment("acme", "po.pdf", "application/pdf", b"x")
        resp = self._gw().execute(_req(f"https://attachments.acme.example/{rec.id}"))
        assert resp.status == "FAILED"
        assert "attachment fetch failed" in (resp.error or "")

    def test_offallowlist_host_blocked_before_store_lookup(self):
        # A hostile manifest URL never reaches the store — SSRF wins first.
        from gateways.attachment_store import store_attachment
        rec = store_attachment("acme", "po.pdf", "application/pdf", b"x")
        url = f"https://evil.example/{rec.id}"
        resp = self._gw().execute(_req(url, tenant_id="acme"))
        assert resp.status == "FAILED"
        assert "SSRF blocked" in (resp.error or "")
