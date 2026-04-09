"""Standard error envelope (architecture_v3.md Section 8.3).

All API error responses use the same JSON structure:

    {
      "error": {
        "code": "SHADOW_BLOCKED",
        "message": "...",
        "trace_id": "...",
        "details": { ... }
      }
    }
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    trace_id: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


class ASOEError(Exception):
    """Base exception for ASOE API errors."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        trace_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.trace_id = trace_id
        self.details = details
        super().__init__(message)


async def asoe_error_handler(_request: Request, exc: ASOEError) -> JSONResponse:
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code=exc.code,
            message=exc.message,
            trace_id=exc.trace_id,
            details=exc.details,
        )
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=envelope.model_dump(exclude_none=True),
    )


async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code="INTERNAL_ERROR",
            message="An unexpected error occurred.",
        )
    )
    return JSONResponse(status_code=500, content=envelope.model_dump(exclude_none=True))
