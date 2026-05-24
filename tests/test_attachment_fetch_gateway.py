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
        fetcher=lambda u: calls.append(u) or {"fetched": True},
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
