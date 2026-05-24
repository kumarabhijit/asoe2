"""ASOE API middleware.

Implements architecture_v3.md §11.4 — X-Trace-ID propagation.

If the client sends an ``X-Trace-ID`` header, it is used. Otherwise,
a UUID is generated at the API boundary. The trace_id is stored in
``request.state.trace_id`` and returned in every response as
``X-Trace-ID``.
"""

from __future__ import annotations

from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


class TraceIDMiddleware(BaseHTTPMiddleware):
    """Propagate or generate X-Trace-ID on every request/response."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        trace_id = request.headers.get("X-Trace-ID") or str(uuid4())
        request.state.trace_id = trace_id

        response = await call_next(request)
        response.headers["X-Trace-ID"] = trace_id
        return response


# DoR #10 — security response headers. ASOE serves a JSON API (responses are
# never executed as a document), so the default CSP locks everything down:
# `default-src 'none'` + no framing + no base-uri. The interactive API docs
# (/docs, /redoc) are HTML pages that load Swagger/ReDoc assets, so they get a
# docs-compatible CSP instead of the strict one. The remaining headers are
# universal hardening (MIME-sniff, clickjacking, referrer leakage).
_STRICT_CSP = (
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'none'"
)
# Swagger UI / ReDoc need self scripts+styles, inline styles, and data: images.
_DOCS_CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'"
)
_DOCS_PATHS = ("/docs", "/redoc")
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach CSP + hardening headers to every response (DoR #10)."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        is_docs = any(request.url.path.startswith(p) for p in _DOCS_PATHS)
        response.headers.setdefault(
            "Content-Security-Policy", _DOCS_CSP if is_docs else _STRICT_CSP
        )
        return response
