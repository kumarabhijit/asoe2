from __future__ import annotations

# Attachment-fetch gateway (DoR #10 — the live wiring of the SSRF guard).
#
# Inbound Customer-Inbox emails carry an `attachment_manifest` of URIs supplied
# by the upstream email-intelligence agent. Fetching those URIs is a classic
# SSRF sink: a hostile sender could point an attachment at an internal address
# (cloud metadata, localhost, a private service). This gateway is the single
# place attachment URLs are retrieved, and it runs every URL through
# `hardening.ssrf.validate_outbound_url` BEFORE any fetch — so the guard is
# wired, not just available.
#
# The actual byte retrieval is injected (`fetcher`); when none is supplied the
# gateway returns a deterministic STUB result (sandbox / tests have no network).
# The SSRF check is real either way — a blocked URL never reaches the fetcher.

from typing import Any, Callable, Dict, Optional

from contracts.models import GatewayRequest, GatewayResponse
from contracts.policy import ATTACHMENT_FETCH_ALLOWED_HOSTS
from hardening.ssrf import SSRFError, validate_outbound_url

GATEWAY_NAME = "attachment_fetch"

# Type of an injected real fetcher: (validated_url) -> response data dict.
Fetcher = Callable[[str], Dict[str, Any]]


class AttachmentFetchGateway:
    """Fetch an email attachment by URL, gated by the SSRF allowlist.

    `execute` expects ``params = {"url": str, "resolve"?: bool}``. `resolve`
    (default False so tests/sandbox stay offline) turns on the DNS-rebinding
    check — a live deployment with a real `fetcher` should pass ``resolve=True``.
    A blocked URL yields ``status="FAILED"`` with the SSRF reason and never
    touches the fetcher.
    """

    def __init__(
        self,
        *,
        allowed_hosts: Optional[frozenset] = None,
        fetcher: Optional[Fetcher] = None,
    ) -> None:
        self._allowed = frozenset(allowed_hosts or ATTACHMENT_FETCH_ALLOWED_HOSTS)
        self._fetcher = fetcher

    @property
    def name(self) -> str:
        return GATEWAY_NAME

    def execute(self, request: GatewayRequest) -> GatewayResponse:
        params = request.params or {}
        url = params.get("url")
        if not url or not isinstance(url, str):
            return GatewayResponse(
                gateway_name=GATEWAY_NAME, operation=request.operation,
                status="FAILED", error="attachment_fetch requires a 'url' param",
            )

        # The SSRF gate — runs BEFORE any retrieval.
        try:
            validate_outbound_url(
                url,
                allowed_hosts=self._allowed,
                resolve=bool(params.get("resolve", False)),
            )
        except SSRFError as exc:
            return GatewayResponse(
                gateway_name=GATEWAY_NAME, operation=request.operation,
                status="FAILED", error=f"SSRF blocked: {exc}",
            )

        # Safe URL — fetch (injected real fetcher, else a deterministic stub).
        if self._fetcher is not None:
            # A real fetcher signals retrieval failures (not-found, timeout,
            # non-200, oversize) by raising — turn that into an explicit FAILED
            # rather than letting it crash the pipeline (CLAUDE.md §5: explicit
            # structured failure, never silent).
            try:
                data = dict(self._fetcher(url))
            except Exception as exc:
                return GatewayResponse(
                    gateway_name=GATEWAY_NAME, operation=request.operation,
                    status="FAILED", error=f"attachment fetch failed: {exc}",
                )
        else:
            data = {
                "url": url,
                "fetched": True,
                "content_type": "application/pdf",
                "bytes": 0,
                "note": "stub fetch (no network in sandbox/tests)",
            }
        return GatewayResponse(
            gateway_name=GATEWAY_NAME, operation=request.operation,
            status="SUCCESS", data=data,
        )

    def health_check(self) -> bool:
        return True
