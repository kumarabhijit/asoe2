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

from api.routes import accounts, auth, exceptions, health, pipeline, policies, workflows, ws
from api.routes import duplicate_envelope as _duplicate_envelope_routes
from api.routes import sandbox as _sandbox_routes


_LOCAL_DEV_ORIGINS = (
    "http://localhost:3000",
    "http://localhost:3100",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3100",
)


def _resolve_cors_config(env: dict[str, str] | None = None) -> tuple[list[str], str | None]:
    """Resolve the CORS allowlist + regex from environment variables.

    Pure function so it is unit-testable without process env mutation.

    Sources, unioned in order:
      * ``CORS_ALLOWED_ORIGINS`` — comma-separated list (preferred form
        for multi-origin: pre-prod UI + Vercel production + custom domain).
      * ``CORS_ALLOWED_ORIGIN`` — single origin (legacy single-value form,
        kept so existing bicep templates / parameter files continue working).
      * Local dev origins (``http://localhost:3000`` etc.) — only when
        ``ASOE_ENV=sandbox``, so production builds never silently allow
        a developer laptop.

    The regex (``CORS_ALLOWED_ORIGIN_REGEX``) is returned separately and
    handed to ``CORSMiddleware``'s ``allow_origin_regex`` so we can match
    Vercel preview URLs (``asoe-ui-git-<branch>-<team>.vercel.app``)
    without listing them individually.
    """
    e = env if env is not None else os.environ
    asoe_env = e.get("ASOE_ENV", "production").lower()

    origins: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> None:
        v = value.strip()
        if v and v not in seen:
            origins.append(v)
            seen.add(v)

    csv = e.get("CORS_ALLOWED_ORIGINS", "")
    if csv:
        for part in csv.split(","):
            _add(part)

    single = e.get("CORS_ALLOWED_ORIGIN", "").strip()
    if single:
        _add(single)

    if asoe_env == "sandbox":
        for o in _LOCAL_DEV_ORIGINS:
            _add(o)

    regex = (e.get("CORS_ALLOWED_ORIGIN_REGEX", "") or "").strip() or None
    return origins, regex


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    application = FastAPI(
        title="ASOE — Agentic System for Order-to-Cash Exceptions",
        version="0.3.2",
        description="Deterministic, compliance-aware exception management API.",
    )

    # CORS — driven by env vars so the bicep template / parameter file is
    # the single source of truth, not a hardcoded list. ``allow_credentials``
    # is required (NextAuth + ``Authorization: Bearer``); that means we can
    # never use ``allow_origins=["*"]``, so the resolver always produces an
    # explicit list (and an optional regex for Vercel preview URLs).
    cors_origins, cors_regex = _resolve_cors_config()
    if cors_origins or cors_regex:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_origin_regex=cors_regex,
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
    # Intent-specific canonical envelopes (ADR-028 G2 / action item A6).
    # Path scheme: /api/v1/exceptions/duplicates/{id} (and future siblings
    # /exceptions/back-orders/{id} etc.). Mounted under the same /api/v1
    # prefix as exceptions.router; each route in the module declares its
    # own absolute path under that prefix.
    application.include_router(
        _duplicate_envelope_routes.router, prefix="/api/v1", tags=["exceptions"],
    )
    application.include_router(workflows.router, prefix="/api/v1", tags=["workflows"])
    application.include_router(policies.router, prefix="/api/v1", tags=["policies"])
    application.include_router(accounts.router, prefix="/api/v1", tags=["accounts"])
    application.include_router(ws.router, prefix="/api/v1", tags=["websocket"])
    application.include_router(pipeline.router, prefix="/api/v1", tags=["pipeline"])
    application.include_router(auth.router, prefix="/api/auth", tags=["auth"])

    # Sandbox-only test-fixture endpoints (Playwright browser e2e). Mounted
    # only when ASOE_ENV=sandbox; each handler additionally re-checks the
    # env at call time (defence in depth against accidental mis-include).
    if os.getenv("ASOE_ENV", "production").lower() == "sandbox":
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
