from __future__ import annotations

# SSRF allowlist for outbound fetches (DoR #10).
#
# The Customer-Inbox flow will eventually fetch email attachments by URL (the
# `attachment_manifest` carries URIs from the upstream email-intelligence agent).
# An attacker who controls an inbound email could point an attachment at an
# internal address (cloud metadata `169.254.169.254`, `localhost`, a private
# 10.x service) to exfiltrate secrets or pivot — the classic SSRF. This module
# is the guard every outbound fetch MUST pass its URL through BEFORE making the
# request.
#
# It is deliberately allowlist-first: a host is fetchable only if it matches the
# caller-supplied `allowed_hosts` (exact or dot-suffix). On top of that it
# rejects, defensively: non-HTTPS schemes, embedded credentials, non-default
# ports, and any host that resolves to / is a private, loopback, link-local,
# or otherwise non-global IP literal.
#
# No live attachment-fetch path exists yet, so nothing calls this in-tree today;
# it ships ready-to-wire so the fetch lands safe-by-construction (with a test
# that locks the threat cases).

import ipaddress
import socket
from urllib.parse import urlsplit

__all__ = ["SSRFError", "validate_outbound_url", "is_safe_outbound_url"]

_ALLOWED_SCHEMES = frozenset({"https"})
_DEFAULT_PORTS = frozenset({None, 443})
# Internal hostnames that must never be fetched even if an operator mistakenly
# allowlists them (these don't parse as IP literals, so the IP check misses them
# when DNS resolution is skipped).
_DENY_HOSTNAMES = frozenset({"localhost"})
_DENY_SUFFIXES = (".localhost", ".local", ".internal")


class SSRFError(ValueError):
    """Raised when an outbound URL fails the SSRF allowlist / safety checks."""


def _host_allowed(host: str, allowed_hosts: frozenset[str]) -> bool:
    host = host.lower().rstrip(".")
    for allowed in allowed_hosts:
        a = allowed.lower().rstrip(".")
        if host == a or host.endswith("." + a):
            return True
    return False


def _ip_is_global(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return True  # not an IP literal — hostname path handles it
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def validate_outbound_url(
    url: str,
    *,
    allowed_hosts,
    resolve: bool = True,
) -> str:
    """Return `url` unchanged if it is safe to fetch, else raise SSRFError.

    Args:
      url: the URL to fetch.
      allowed_hosts: iterable of permitted hostnames (exact or dot-suffix, e.g.
        ``s3.amazonaws.com`` matches ``acme.s3.amazonaws.com``). REQUIRED and
        non-empty — there is no implicit "allow all".
      resolve: when True, DNS-resolve the host and reject if ANY resolved
        address is non-global (defends against DNS-rebinding to a private IP).
    """
    allowed = frozenset(h.lower().rstrip(".") for h in (allowed_hosts or ()))
    if not allowed:
        raise SSRFError("no allowed_hosts configured — refusing every fetch")

    parts = urlsplit(url)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise SSRFError(f"scheme {parts.scheme!r} not allowed (https only)")
    if parts.username or parts.password:
        raise SSRFError("embedded credentials are not allowed in fetch URLs")
    host = parts.hostname
    if not host:
        raise SSRFError("URL has no host")
    try:
        port = parts.port
    except ValueError as exc:  # malformed port
        raise SSRFError(f"invalid port: {exc}")
    if port not in _DEFAULT_PORTS:
        raise SSRFError(f"port {port} not allowed (443 only)")

    host_l = host.lower().rstrip(".")
    if host_l in _DENY_HOSTNAMES or host_l.endswith(_DENY_SUFFIXES):
        raise SSRFError(f"host {host!r} is an internal name")

    if not _host_allowed(host, allowed):
        raise SSRFError(f"host {host!r} is not on the fetch allowlist")

    # Direct IP literal in the URL → must be global.
    if not _ip_is_global(host):
        raise SSRFError(f"host {host!r} resolves to a non-global address")

    # DNS-rebinding guard: every resolved address must be global.
    if resolve:
        try:
            infos = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            raise SSRFError(f"host {host!r} did not resolve: {exc}")
        for info in infos:
            addr = info[4][0]
            if not _ip_is_global(addr):
                raise SSRFError(
                    f"host {host!r} resolves to non-global address {addr}"
                )
    return url


def is_safe_outbound_url(url: str, *, allowed_hosts, resolve: bool = True) -> bool:
    """Boolean convenience wrapper around `validate_outbound_url`."""
    try:
        validate_outbound_url(url, allowed_hosts=allowed_hosts, resolve=resolve)
        return True
    except SSRFError:
        return False
