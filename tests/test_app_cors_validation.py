"""PARITY-0 Phase 0a — CORS regex validation at startup.

Per the Frontend + Security review of the parity plan: the app must
reject an overly permissive ``CORS_ALLOWED_ORIGIN_REGEX`` at boot time
rather than silently allow arbitrary origins. The Vercel preview-URL
pattern is the canonical good case; ``.*`` / ``^.*$`` are the
canonical bad cases. Invalid regex syntax also fails loud.

The resolved CORS allowlist + regex are logged at INFO on every boot so
ops can audit what's allowed in each deploy.
"""
from __future__ import annotations

import logging

import pytest

from api.app import _resolve_cors_config, _validate_cors_regex


# ── Unit tests on the pure validator ────────────────────────────────────────

@pytest.mark.parametrize("bad_regex", [
    r".*",
    r".+",
    r"^.*$",
    r"^.+$",
    r"https?://.*",       # any URL — no host constraint
    r".*\.example\.com",   # leading wildcard makes it accept anything
    # SaaS multi-tenant footgun: regex matches the legitimate Vercel
    # FQDN AND any attacker-controlled Vercel subdomain (Review 1
    # finding).
    r"^https?://.*\.vercel\.app$",
    r"^https://.*\.azurecontainerapps\.io$",
    r"^https://[\w-]+\.azurewebsites\.net$",
])
def test_validator_rejects_obviously_unsafe_regex(bad_regex):
    with pytest.raises(RuntimeError) as exc_info:
        _validate_cors_regex(bad_regex)
    assert "arbitrary" in str(exc_info.value).lower() or "permissive" in str(exc_info.value).lower()


@pytest.mark.parametrize("good_regex", [
    r"^https://asoe-ui-git-[\w-]+-asoe-team\.vercel\.app$",
    r"^https://[\w-]+\.preprod\.asoe\.example\.com$",
    r"^https://asoe-ui\.azurecontainerapps\.io$",
])
def test_validator_accepts_host_pinned_patterns(good_regex):
    # Must not raise.
    _validate_cors_regex(good_regex)


def test_validator_rejects_invalid_regex():
    with pytest.raises(RuntimeError) as exc_info:
        _validate_cors_regex(r"^https://[unclosed")
    assert "regex" in str(exc_info.value).lower()


def test_validator_noop_on_empty():
    _validate_cors_regex(None)
    _validate_cors_regex("")


# ── App-boot tests: refuse to boot on unsafe regex ─────────────────────────

def test_app_boot_refuses_overly_permissive_regex(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGIN_REGEX", r".*")
    from api.app import create_app
    with pytest.raises(RuntimeError):
        create_app()


def test_app_boot_accepts_vercel_pattern(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGIN_REGEX",
        r"^https://asoe-ui-git-[\w-]+-asoe-team\.vercel\.app$",
    )
    from api.app import create_app
    create_app()  # must not raise


def test_resolved_cors_logged_at_info_on_boot(monkeypatch, caplog):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://asoe-ui.azurecontainerapps.io")
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGIN_REGEX",
        r"^https://[\w-]+\.preprod\.asoe\.example\.com$",
    )
    caplog.set_level(logging.INFO, logger="asoe.api.app")
    from api.app import create_app
    create_app()
    msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "CORS" in msgs or "cors" in msgs
    assert "asoe-ui.azurecontainerapps.io" in msgs


# ── Pure-function fallthrough — empty regex still resolves cleanly ─────────

def test_resolve_cors_config_handles_no_regex(monkeypatch):
    monkeypatch.delenv("CORS_ALLOWED_ORIGIN_REGEX", raising=False)
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://example.com")
    monkeypatch.setenv("ASOE_ENV", "preprod")
    origins, regex = _resolve_cors_config()
    assert "https://example.com" in origins
    assert regex is None
