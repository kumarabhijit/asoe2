"""Tests for env-driven JWT token TTL resolution.

Regression: ACCESS_TOKEN_EXPIRE_SECONDS was a hardcoded 15 * 60 (15
minutes). The asoe-ui ExceptionDetailPanel rendered "Exception not
found" after a silent 401 when the operator's session expired -- a
poor sandbox UX. The fix: env-aware defaults (sandbox=24h,
production=60min) overridable via env vars per deployment.

Both halves matter:
  * Sandbox demos shouldn't 401 mid-coffee-break.
  * Production short-lived access tokens (with refresh rotation) are
    the standard architecture pattern; we don't want to silently drift
    everyone to 24h-access tokens just to fix the sandbox UX.
"""

from __future__ import annotations

from api.deps import _resolve_token_ttls


def test_sandbox_defaults_are_long_enough_for_demo_sessions(monkeypatch):
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    monkeypatch.delenv("ASOE_ACCESS_TOKEN_TTL_SECONDS", raising=False)
    monkeypatch.delenv("ASOE_REFRESH_TOKEN_TTL_SECONDS", raising=False)
    access, refresh = _resolve_token_ttls()
    assert access == 24 * 3600
    assert refresh == 30 * 24 * 3600


def test_production_defaults_are_short_lived_with_long_refresh(monkeypatch):
    monkeypatch.setenv("ASOE_ENV", "production")
    monkeypatch.delenv("ASOE_ACCESS_TOKEN_TTL_SECONDS", raising=False)
    monkeypatch.delenv("ASOE_REFRESH_TOKEN_TTL_SECONDS", raising=False)
    access, refresh = _resolve_token_ttls()
    assert access == 60 * 60
    assert refresh == 7 * 24 * 3600


def test_unrecognised_env_treated_as_production(monkeypatch):
    monkeypatch.setenv("ASOE_ENV", "qa")
    monkeypatch.delenv("ASOE_ACCESS_TOKEN_TTL_SECONDS", raising=False)
    monkeypatch.delenv("ASOE_REFRESH_TOKEN_TTL_SECONDS", raising=False)
    access, _refresh = _resolve_token_ttls()
    assert access == 60 * 60


def test_explicit_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv("ASOE_ENV", "production")
    monkeypatch.setenv("ASOE_ACCESS_TOKEN_TTL_SECONDS", "300")
    monkeypatch.setenv("ASOE_REFRESH_TOKEN_TTL_SECONDS", "86400")
    access, refresh = _resolve_token_ttls()
    assert access == 300
    assert refresh == 86_400


def test_override_works_in_sandbox_too(monkeypatch):
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    monkeypatch.setenv("ASOE_ACCESS_TOKEN_TTL_SECONDS", "900")
    access, _refresh = _resolve_token_ttls()
    assert access == 900


def test_missing_env_var_falls_back_to_production_defaults(monkeypatch):
    monkeypatch.delenv("ASOE_ENV", raising=False)
    monkeypatch.delenv("ASOE_ACCESS_TOKEN_TTL_SECONDS", raising=False)
    access, refresh = _resolve_token_ttls()
    assert access == 60 * 60
    assert refresh == 7 * 24 * 3600


def test_empty_string_is_treated_as_default_not_zero(monkeypatch):
    """Bicep declares the env var unconditionally with default ''.
    An empty value must mean 'use env-driven default', NOT 'TTL=0'."""
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    monkeypatch.setenv("ASOE_ACCESS_TOKEN_TTL_SECONDS", "")
    monkeypatch.setenv("ASOE_REFRESH_TOKEN_TTL_SECONDS", "")
    access, refresh = _resolve_token_ttls()
    assert access == 24 * 3600
    assert refresh == 30 * 24 * 3600


def test_whitespace_only_treated_as_unset(monkeypatch):
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    monkeypatch.setenv("ASOE_ACCESS_TOKEN_TTL_SECONDS", "   ")
    access, _ = _resolve_token_ttls()
    assert access == 24 * 3600


def test_malformed_override_falls_back_to_default(monkeypatch):
    """Hand-edit accidents (e.g. 'one_hour') must not crash startup."""
    monkeypatch.setenv("ASOE_ENV", "production")
    monkeypatch.setenv("ASOE_ACCESS_TOKEN_TTL_SECONDS", "one_hour")
    access, _ = _resolve_token_ttls()
    assert access == 60 * 60


def test_zero_or_negative_override_falls_back_to_default(monkeypatch):
    """A zero / negative TTL is meaningless; use the env default."""
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    monkeypatch.setenv("ASOE_ACCESS_TOKEN_TTL_SECONDS", "0")
    access, _ = _resolve_token_ttls()
    assert access == 24 * 3600
    monkeypatch.setenv("ASOE_ACCESS_TOKEN_TTL_SECONDS", "-1")
    access, _ = _resolve_token_ttls()
    assert access == 24 * 3600


def test_user_facing_presets_round_trip(monkeypatch):
    """The three operator-friendly presets we expose in the README:
    900 (15min), 3600 (1h), 86400 (24h)."""
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    for preset in (900, 3600, 86_400):
        monkeypatch.setenv("ASOE_ACCESS_TOKEN_TTL_SECONDS", str(preset))
        access, _ = _resolve_token_ttls()
        assert access == preset
