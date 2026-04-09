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
