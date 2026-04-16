"""FastAPI application factory (architecture_v3.md Section 4.2, 8, 10).

Creates the ASOE API server with all routes mounted:
  /api/v1/*   — business endpoints (health, exceptions, workflows, policies)
  /api/v1/ws  — WebSocket hub for real-time event streaming (§10)
  /api/auth/* — authentication endpoints (login, SSO, MFA, refresh, me)

Usage:
    uvicorn api.app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from fastapi import FastAPI

from api.errors import ASOEError, asoe_error_handler, unhandled_error_handler
from api.middleware import TraceIDMiddleware
from api.routes import accounts, auth, exceptions, health, policies, workflows, ws


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    application = FastAPI(
        title="ASOE — Agentic System for Order-to-Cash Exceptions",
        version="0.3.2",
        description="Deterministic, compliance-aware exception management API.",
    )

    # Middleware (§11.4 — X-Trace-ID propagation)
    application.add_middleware(TraceIDMiddleware)

    # Register error handlers
    application.add_exception_handler(ASOEError, asoe_error_handler)  # type: ignore[arg-type]
    application.add_exception_handler(Exception, unhandled_error_handler)  # type: ignore[arg-type]

    # Mount route groups
    application.include_router(health.router, prefix="/api/v1", tags=["health"])
    application.include_router(exceptions.router, prefix="/api/v1", tags=["exceptions"])
    application.include_router(workflows.router, prefix="/api/v1", tags=["workflows"])
    application.include_router(policies.router, prefix="/api/v1", tags=["policies"])
    application.include_router(accounts.router, prefix="/api/v1", tags=["accounts"])
    application.include_router(ws.router, prefix="/api/v1", tags=["websocket"])
    application.include_router(auth.router, prefix="/api/auth", tags=["auth"])

    return application


app = create_app()
