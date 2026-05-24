"""DoR gate #10 (SSRF half) — outbound-fetch allowlist guard.

Locks the threat cases for `hardening.ssrf.validate_outbound_url`: only allowed
HTTPS hosts on the default port pass; internal/metadata addresses, non-HTTPS
schemes, embedded credentials, odd ports, and off-allowlist hosts are refused.
`resolve=False` is used so the tests are deterministic + offline (the DNS-
rebinding guard is exercised separately against IP literals).
"""

from __future__ import annotations

import pytest

from hardening.ssrf import SSRFError, is_safe_outbound_url, validate_outbound_url

_ALLOW = {"s3.amazonaws.com", "mail.acme.example"}


def test_allows_an_allowlisted_https_host():
    url = "https://acme.s3.amazonaws.com/att/po.pdf"  # dot-suffix match
    assert validate_outbound_url(url, allowed_hosts=_ALLOW, resolve=False) == url
    assert is_safe_outbound_url("https://mail.acme.example/a", allowed_hosts=_ALLOW, resolve=False)


@pytest.mark.parametrize("url", [
    "http://mail.acme.example/a",                 # non-https
    "https://mail.acme.example:8080/a",           # non-default port
    "https://user:pass@mail.acme.example/a",      # embedded credentials
    "https://evil.example/a",                     # off-allowlist host
    "ftp://mail.acme.example/a",                  # non-https scheme
])
def test_rejects_unsafe_urls(url):
    assert not is_safe_outbound_url(url, allowed_hosts=_ALLOW, resolve=False)
    with pytest.raises(SSRFError):
        validate_outbound_url(url, allowed_hosts=_ALLOW, resolve=False)


@pytest.mark.parametrize("host", [
    "127.0.0.1", "localhost", "169.254.169.254", "10.0.0.5", "192.168.1.1",
    "[::1]",
])
def test_rejects_internal_addresses_even_if_allowlisted(host):
    # Even if the operator mistakenly allowlists an internal host, the
    # non-global-address check refuses it.
    allow = {host.strip("[]"), *_ALLOW}
    url = f"https://{host}/latest/meta-data/"
    assert not is_safe_outbound_url(url, allowed_hosts=allow, resolve=False)


def test_empty_allowlist_refuses_everything():
    with pytest.raises(SSRFError):
        validate_outbound_url("https://s3.amazonaws.com/x", allowed_hosts=set(), resolve=False)
