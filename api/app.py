"""FastAPI application factory (architecture_v3.md Section 4.2, 8, 10).

Creates the ASOE API server with all routes mounted:
  /api/v1/*   — business endpoints (health, exceptions, workflows, policies)
  /api/v1/ws  — WebSocket hub for real-time event streaming (§10)
  /api/auth/* — authentication endpoints (login, SSO, MFA, refresh, me)

Usage:
    uvicorn api.app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.errors import ASOEError, asoe_error_handler, unhandled_error_handler
from api.middleware import SecurityHeadersMiddleware, TraceIDMiddleware
import os

logger = logging.getLogger("asoe.api.app")

from api.routes import accounts, auth, cases, exceptions, health, pipeline, policies, workflows, ws
from api.routes import attachments as _attachments_routes
from api.routes import config as _config_routes
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


# Sentinel hosts the regex must NOT match. If the regex would accept these,
# it's permissive enough that an attacker-controlled domain would slip
# through — refuse to boot.
#
# Two probe families:
#   1. Non-routable example.* / .invalid hosts catch unconstrained `.*`
#      and `https?://.*` patterns.
#   2. Subdomains of well-known SaaS multi-tenant domains (Vercel,
#      Azure Container Apps, Azure App Service, Heroku, GitHub Pages,
#      AWS CloudFront) catch the "wildcard over a shared SaaS host"
#      footgun — e.g. `^https?://.*\.vercel\.app$` allows ANY Vercel app
#      to call this API, including an attacker's. The legitimate
#      preprod URL must be pinned to a specific subdomain, not a TLD
#      wildcard.
#
# A custom domain like `^https://[\w-]+\.preprod\.example\.com$` will
# legitimately accept `attacker.preprod.example.com` — we can't catch
# that without knowing operator intent. Documented limitation: the
# operator owns the customer-domain wildcard surface; we only catch the
# SaaS multi-tenant gotchas.
_CORS_PERMISSIVE_PROBES: tuple[str, ...] = (
    "https://attacker.example",
    "https://attacker.example.com",
    "https://anything-random-xyz.example.org",
    "http://evil.invalid",
    "https://attacker.vercel.app",
    "https://attacker.azurecontainerapps.io",
    "https://attacker.azurewebsites.net",
    "https://attacker.herokuapp.com",
    "https://attacker.github.io",
    "https://attacker.cloudfront.net",
)


def _validate_cors_regex(regex: str | None) -> None:
    """Reject overly-permissive or syntactically-invalid CORS regex at boot.

    Per the Frontend + Security review of the v3 parity plan: a regex that
    matches arbitrary origins (``.*``, ``^.*$``, ``https?://.*``, …) is a
    silent bypass of the allowlist. Refuse to boot rather than serve that.
    A regex with a leading wildcard like ``.*\\.example\\.com`` also accepts
    "https://attacker.example.com" — caught by the same probe.

    No-op on None/empty so the existing no-regex deployments stay green.
    """
    if not regex:
        return
    try:
        compiled = re.compile(regex)
    except re.error as exc:
        raise RuntimeError(
            f"CORS_ALLOWED_ORIGIN_REGEX={regex!r} is not a valid regex: {exc}"
        ) from exc
    for probe in _CORS_PERMISSIVE_PROBES:
        if compiled.fullmatch(probe):
            raise RuntimeError(
                f"CORS_ALLOWED_ORIGIN_REGEX={regex!r} matches arbitrary "
                f"origins (e.g. {probe!r}) — overly permissive. Pin to "
                f"specific hosts (e.g. "
                f"'^https://[\\w-]+\\.preprod\\.asoe\\.example\\.com$')."
            )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """App lifespan — starts the opt-in outbox reconcile scheduler (DoR #6).

    Disabled by default; only runs when ASOE_OUTBOX_RECONCILE_INTERVAL_S is set
    (see orchestration/outbox_scheduler). No-op otherwise, so the default
    runtime + tests are unaffected."""
    from orchestration.outbox_scheduler import start_if_configured

    task = None
    try:
        task = start_if_configured()
    except Exception:  # pragma: no cover - scheduler must never block startup
        logging.getLogger("asoe").exception("outbox scheduler failed to start")
    try:
        yield
    finally:
        if task is not None:
            task.cancel()


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    application = FastAPI(
        title="ASOE — Agentic System for Order-to-Cash Exceptions",
        version="0.3.2",
        description="Deterministic, compliance-aware exception management API.",
        lifespan=_lifespan,
    )

    cors_origins, cors_regex = _resolve_cors_config()
    # PARITY-0 Phase 0a — refuse to boot on a permissive / invalid regex
    # rather than silently serve it (Frontend + Security review).
    _validate_cors_regex(cors_regex)
    # Log the resolved allowlist at INFO on every boot so ops can audit
    # exactly which origins each deploy accepts. Cheap, one line per boot.
    logger.info(
        "resolved CORS allowlist: origins=%s regex=%s",
        cors_origins or "(none)", cors_regex or "(none)",
    )
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

    application.add_middleware(TraceIDMiddleware)
    application.add_middleware(SecurityHeadersMiddleware)

    application.add_exception_handler(ASOEError, asoe_error_handler)  # type: ignore[arg-type]
    application.add_exception_handler(Exception, unhandled_error_handler)  # type: ignore[arg-type]

    application.include_router(health.router, prefix="/api/v1", tags=["health"])
    application.include_router(exceptions.router, prefix="/api/v1", tags=["exceptions"])
    application.include_router(
        _duplicate_envelope_routes.router, prefix="/api/v1", tags=["exceptions"],
    )
    # ADR-038 Phase H.6 — case-centric read surface (`/cases` UI).
    application.include_router(cases.router, prefix="/api/v1", tags=["cases"])
    application.include_router(_attachments_routes.router, prefix="/api/v1", tags=["attachments"])
    application.include_router(workflows.router, prefix="/api/v1", tags=["workflows"])
    application.include_router(policies.router, prefix="/api/v1", tags=["policies"])
    application.include_router(accounts.router, prefix="/api/v1", tags=["accounts"])
    application.include_router(ws.router, prefix="/api/v1", tags=["websocket"])
    application.include_router(pipeline.router, prefix="/api/v1", tags=["pipeline"])
    application.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    # ADR-030 / A9 — tenant-config admin surface (PR-C.2). Five endpoints
    # under /api/v1/config/tenants/{tenant_id}/**. Audit-chain wiring
    # uses the existing PolicyRepository hash chain.
    application.include_router(
        _config_routes.router, prefix="/api/v1", tags=["config"],
    )

    _register_gateways_for_env(application)

    return application


_RECOGNISED_ENVS = ("sandbox", "preprod", "production")


def _register_gateways_for_env(application: FastAPI) -> None:
    """Env-driven gateway registration (PARITY-0 Phase 0b).

    * ``sandbox`` — mount sandbox fixture routes + register the stub set.
    * ``preprod`` — register the stub set (or, post-Phase-6, real
      connectors swapped in one-by-one). No sandbox routes mounted.
    * ``production`` — register real connectors (raises until wired).
    * Any other value — refuse to boot rather than silently using an
      empty gateway registry. Per the v3 parity plan (Phase 0b,
      Security + Azure/SRE review), any non-sandbox env requires
      explicit registration; an unknown value is a misconfigured deploy.
    """
    asoe_env = os.getenv("ASOE_ENV", "production").lower()
    if asoe_env == "sandbox":
        application.include_router(
            _sandbox_routes.router, prefix="/api/v1", tags=["sandbox"],
        )
        from api.sandbox_gateways import register_sandbox_gateways
        register_sandbox_gateways()
    elif asoe_env == "preprod":
        from api.preprod_gateways import register_preprod_gateways
        register_preprod_gateways()
    elif asoe_env == "production":
        from api.production_gateways import register_production_gateways
        register_production_gateways()
    else:
        raise RuntimeError(
            f"Unknown ASOE_ENV={asoe_env!r}. Recognised values: "
            f"{', '.join(_RECOGNISED_ENVS)}. Each non-sandbox env "
            f"requires an explicit gateway registration; see "
            f"api/preprod_gateways.py and api/production_gateways.py."
        )


app = create_app()
