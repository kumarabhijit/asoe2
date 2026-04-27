"""Tests for env-driven CORS configuration in api.app.

The CORS allowlist must be sourced from environment variables (so the
bicep template / parameter file is the single source of truth) and never
from a hardcoded list. Local-dev origins must only leak in when
``ASOE_ENV=sandbox``.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


def _resolve(env: dict[str, str]):
    """Re-import api.app fresh so we exercise ``_resolve_cors_config`` directly."""
    mod = importlib.import_module("api.app")
    return mod._resolve_cors_config(env)


def test_no_env_yields_no_cors_in_production():
    origins, regex = _resolve({"ASOE_ENV": "production"})
    assert origins == []
    assert regex is None


def test_sandbox_env_unions_local_dev_origins():
    origins, regex = _resolve({"ASOE_ENV": "sandbox"})
    assert "http://localhost:3000" in origins
    assert "http://localhost:3100" in origins
    assert "http://127.0.0.1:3000" in origins
    assert "http://127.0.0.1:3100" in origins
    assert regex is None


def test_legacy_single_origin_var_is_honoured():
    origins, _ = _resolve(
        {"ASOE_ENV": "production", "CORS_ALLOWED_ORIGIN": "https://asoe-ui.vercel.app"}
    )
    assert origins == ["https://asoe-ui.vercel.app"]


def test_csv_var_takes_multiple_origins_in_order():
    origins, _ = _resolve(
        {
            "ASOE_ENV": "production",
            "CORS_ALLOWED_ORIGINS": "https://a.example, https://b.example",
        }
    )
    assert origins == ["https://a.example", "https://b.example"]


def test_csv_and_legacy_var_both_apply_without_duplicates():
    origins, _ = _resolve(
        {
            "ASOE_ENV": "sandbox",
            "CORS_ALLOWED_ORIGINS": "https://asoeprep.example",
            "CORS_ALLOWED_ORIGIN": "https://asoe-ui.vercel.app",
        }
    )
    assert origins[0] == "https://asoeprep.example"
    assert "https://asoe-ui.vercel.app" in origins
    # Sandbox dev origins still unioned in.
    assert "http://localhost:3000" in origins
    # No origin appears twice even if redundantly listed.
    assert len(origins) == len(set(origins))


def test_regex_is_returned_separately():
    origins, regex = _resolve(
        {
            "ASOE_ENV": "production",
            "CORS_ALLOWED_ORIGINS": "https://asoe-ui.vercel.app",
            "CORS_ALLOWED_ORIGIN_REGEX": r"^https://asoe-ui-git-[a-z0-9-]+\.vercel\.app$",
        }
    )
    assert origins == ["https://asoe-ui.vercel.app"]
    assert regex == r"^https://asoe-ui-git-[a-z0-9-]+\.vercel\.app$"


def test_blank_csv_entries_are_ignored():
    origins, _ = _resolve(
        {"ASOE_ENV": "production", "CORS_ALLOWED_ORIGINS": ",, https://x.example , "}
    )
    assert origins == ["https://x.example"]


# ---------------------------------------------------------------------------
# Wiring test: prove the resolver is actually plumbed into the FastAPI app.
# ---------------------------------------------------------------------------


@pytest.fixture
def cors_app(monkeypatch):
    """Build a fresh ``api.app:create_app`` with CORS env vars set.

    ``create_app`` reads env at call time, so we set vars then re-create.
    """
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://asoe-ui.vercel.app")
    # Reload to ensure module-level state is fresh (sandbox routes branch).
    import api.app as app_mod

    importlib.reload(app_mod)
    return app_mod.create_app()


def test_preflight_from_allowed_origin_succeeds(cors_app):
    client = TestClient(cors_app)
    res = client.options(
        "/api/v1/health",
        headers={
            "Origin": "https://asoe-ui.vercel.app",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    # CORSMiddleware returns 200 with the matching ACAO header on success.
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == "https://asoe-ui.vercel.app"
    assert res.headers.get("access-control-allow-credentials") == "true"


def test_preflight_from_unknown_origin_is_rejected(cors_app):
    client = TestClient(cors_app)
    res = client.options(
        "/api/v1/health",
        headers={
            "Origin": "https://attacker.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    # Either no ACAO header, or a non-matching one. Starlette returns 400.
    assert res.headers.get("access-control-allow-origin") != "https://attacker.example"
