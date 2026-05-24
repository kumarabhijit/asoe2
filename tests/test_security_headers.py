"""DoR gate #10 (partial) — security response headers + CSP.

ASOE's API is JSON-only, so the default CSP is `default-src 'none'` — defense in
depth against any response being coerced into an executable document. The
interactive docs (/docs, /redoc) get a Swagger/ReDoc-compatible CSP instead.
Standard hardening headers (nosniff, frame DENY, referrer) ride on every
response. (SSRF allowlist on attachment fetch is deferred — there is no live
attachment-fetch path yet; this gate covers the XSS/CSP half.)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.store import exception_store


@pytest.fixture()
def client():
    exception_store.clear()
    return TestClient(create_app(), raise_server_exceptions=False)


def test_api_responses_carry_strict_csp_and_hardening_headers(client: TestClient):
    res = client.get("/api/v1/health")
    assert res.status_code == 200
    assert res.headers["Content-Security-Policy"] == (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'none'"
    )
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["Referrer-Policy"] == "no-referrer"
    assert res.headers["Cross-Origin-Resource-Policy"] == "same-origin"


def test_headers_present_on_error_responses(client: TestClient):
    # A 401/404 must still be hardened — error pages are a classic gap.
    res = client.get("/api/v1/exceptions/does-not-exist/analysis")
    assert res.status_code in (401, 403, 404)
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in res.headers


def test_docs_get_a_swagger_compatible_csp(client: TestClient):
    res = client.get("/docs")
    if res.status_code != 200:
        pytest.skip("interactive docs not mounted in this configuration")
    csp = res.headers["Content-Security-Policy"]
    # Docs need self scripts/styles; must NOT be the strict default-src 'none'.
    assert "script-src 'self'" in csp
    assert csp != "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
