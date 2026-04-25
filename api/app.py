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
from fastapi.middleware.cors import CORSMiddleware

from api.errors import ASOEError, asoe_error_handler, unhandled_error_handler
from api.middleware import TraceIDMiddleware
import os

from api.routes import accounts, auth, exceptions, health, policies, workflows, ws
from api.routes import sandbox as _sandbox_routes


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    application = FastAPI(
        title="ASOE — Agentic System for Order-to-Cash Exceptions",
        version="0.3.2",
        description="Deterministic, compliance-aware exception management API.",
    )

    # Sandbox-only CORS — production fronts this service with an API
    # gateway + same-origin UI, so Access-Control-Allow-Origin is not
    # needed there. In sandbox the Next dev server runs on localhost:3100
    # and the UI makes cross-origin fetch() calls to localhost:8000; the
    # browser rejects those without explicit CORS. Allowlist is the
    # common local dev origins only — not a wildcard, so accidental
    # production mis-configuration still blocks foreign origins.
    if os.getenv("ASOE_ENV", "sandbox").lower() == "sandbox":
        application.add_middleware(
            CORSMiddleware,
            allow_origins=[
                "http://localhost:3000",
                "http://localhost:3100",
                "http://127.0.0.1:3000",
                "http://127.0.0.1:3100",
            ],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Trace-ID"],
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

    # Sandbox-only test-fixture endpoints (Playwright browser e2e). Mounted
    # only when ASOE_ENV=sandbox; each handler additionally re-checks the
    # env at call time (defence in depth against accidental mis-include).
    if os.getenv("ASOE_ENV", "sandbox").lower() == "sandbox":
        application.include_router(
            _sandbox_routes.router, prefix="/api/v1", tags=["sandbox"],
        )
        # Register stub gateways so recipes that declare GatewayDependency
        # entries (oms / sap_doc / sap_contract / sap_block / sla_contract /
        # sap_customer_master / promotion / buyer_notification) resolve at
        # runtime. In production the platform team wires real adapters at
        # startup; in sandbox these mirror tests/conftest.py.
        from api.sandbox_gateways import register_sandbox_gateways
        register_sandbox_gateways()

    return application


app = create_app()
