from __future__ import annotations

# Router coverage — back-compat + new per-task routing.
#
# Verifies:
#   - Default (no env) returns DeterministicFallbackBackend
#   - USE_OUTLINES_BACKEND=0 also returns the fallback
#   - OutlinesConstrainedBackend is NOT imported at module level
#   - The constraints package __init__ does not export Outlines
#   - ASOE_LLM_PROVIDER=fallback returns fallback explicitly
#   - ASOE_LLM_PROVIDER=anthropic builds a RemoteLLMBackend (with
#     stubbed SDK)
#   - ASOE_LLM_PROVIDER=<bogus> falls closed to fallback
#   - Per-task overrides take precedence over the global default
#   - ASOE_LLM_DISABLE_FOR=intent forces task='intent' to fallback
#     even when the global provider is set to anthropic
#   - Build-time errors (missing key, kill switch, NotImplementedError
#     stub provider) are caught and the router returns the fallback

import sys
from unittest import mock

import pytest

from constraints.fallback_backend import DeterministicFallbackBackend
from constraints.router import get_constrained_backend


# ---------------------------------------------------------------------------
# Back-compat — the existing call sites still work
# ---------------------------------------------------------------------------


def test_router_defaults_to_fallback(monkeypatch):
    monkeypatch.delenv("ASOE_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("USE_OUTLINES_BACKEND", raising=False)
    backend = get_constrained_backend()
    assert isinstance(backend, DeterministicFallbackBackend)


def test_router_returns_fallback_when_disabled(monkeypatch):
    monkeypatch.delenv("ASOE_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("USE_OUTLINES_BACKEND", "0")
    backend = get_constrained_backend()
    assert isinstance(backend, DeterministicFallbackBackend)


def test_outlines_backend_not_imported_at_module_level():
    import constraints.router as router_mod
    assert not hasattr(router_mod, "OutlinesConstrainedBackend")


def test_constraints_package_does_not_expose_outlines_backend():
    import constraints as pkg
    assert not hasattr(pkg, "OutlinesConstrainedBackend")


# ---------------------------------------------------------------------------
# ASOE_LLM_PROVIDER — explicit values
# ---------------------------------------------------------------------------


def test_explicit_fallback_returns_deterministic(monkeypatch):
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "fallback")
    backend = get_constrained_backend()
    assert isinstance(backend, DeterministicFallbackBackend)


def test_unknown_provider_falls_closed_to_fallback(monkeypatch):
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "totally-bogus")
    backend = get_constrained_backend()
    assert isinstance(backend, DeterministicFallbackBackend)


def test_anthropic_provider_builds_remote_backend(monkeypatch):
    """When ASOE_LLM_PROVIDER=anthropic and the SDK is available, the
    router returns a RemoteLLMBackend wrapping an Anthropic client."""
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")
    monkeypatch.setenv("ASOE_ENV", "sandbox")

    fake = mock.Mock()
    fake.Anthropic = mock.Mock(return_value=mock.Mock())
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    from constraints.llm_backend import RemoteLLMBackend
    backend = get_constrained_backend()
    assert isinstance(backend, RemoteLLMBackend)


def test_anthropic_with_missing_key_falls_back(monkeypatch):
    """ValueError from RemoteLLMConfig (missing api_key) is caught
    and the router serves the deterministic fallback instead."""
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    backend = get_constrained_backend()
    assert isinstance(backend, DeterministicFallbackBackend)


def test_kill_switch_during_build_falls_back(monkeypatch):
    """If ASOE_KILL_SWITCH is active, the Anthropic client raises
    RuntimeError at construction — router catches and falls back."""
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ASOE_KILL_SWITCH", "1")
    backend = get_constrained_backend()
    assert isinstance(backend, DeterministicFallbackBackend)


def test_stub_provider_falls_back(monkeypatch):
    """OpenAI / Google / Ollama / HuggingFace are V1 stubs that raise
    NotImplementedError at from_config. Router catches and falls back."""
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://my-azure.example/")
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    backend = get_constrained_backend()
    assert isinstance(backend, DeterministicFallbackBackend)


# ---------------------------------------------------------------------------
# Per-task overrides
# ---------------------------------------------------------------------------


def test_per_task_override_takes_precedence(monkeypatch):
    """Global=fallback but ASOE_LLM_PROVIDER_INTENT=anthropic →
    task='intent' resolves to anthropic."""
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "fallback")
    monkeypatch.setenv("ASOE_LLM_PROVIDER_INTENT", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")
    monkeypatch.setenv("ASOE_ENV", "sandbox")

    fake = mock.Mock()
    fake.Anthropic = mock.Mock(return_value=mock.Mock())
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    from constraints.llm_backend import RemoteLLMBackend
    intent_backend = get_constrained_backend(task="intent")
    recipe_backend = get_constrained_backend(task="recipe")
    assert isinstance(intent_backend, RemoteLLMBackend)
    # recipe falls back to global=fallback
    assert isinstance(recipe_backend, DeterministicFallbackBackend)


def test_no_task_arg_ignores_per_task_envs(monkeypatch):
    """get_constrained_backend() without a task arg must NOT pick up
    per-task env overrides (back-compat for legacy call sites)."""
    monkeypatch.setenv("ASOE_LLM_PROVIDER_INTENT", "anthropic")
    monkeypatch.delenv("ASOE_LLM_PROVIDER", raising=False)
    backend = get_constrained_backend()  # no task
    assert isinstance(backend, DeterministicFallbackBackend)


# ---------------------------------------------------------------------------
# Runtime kill-by-task — ASOE_LLM_DISABLE_FOR
# ---------------------------------------------------------------------------


def test_disable_for_overrides_provider(monkeypatch):
    """ASOE_LLM_DISABLE_FOR=shadow forces task='shadow' to deterministic
    even when ASOE_LLM_PROVIDER=anthropic is set."""
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ASOE_LLM_DISABLE_FOR", "shadow")
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")
    monkeypatch.setenv("ASOE_ENV", "sandbox")

    fake = mock.Mock()
    fake.Anthropic = mock.Mock(return_value=mock.Mock())
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    from constraints.llm_backend import RemoteLLMBackend
    intent_backend = get_constrained_backend(task="intent")
    shadow_backend = get_constrained_backend(task="shadow")
    assert isinstance(intent_backend, RemoteLLMBackend)
    assert isinstance(shadow_backend, DeterministicFallbackBackend)


def test_disable_for_multi_task(monkeypatch):
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ASOE_LLM_DISABLE_FOR", "intent, shadow")
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")
    monkeypatch.setenv("ASOE_ENV", "sandbox")

    fake = mock.Mock()
    fake.Anthropic = mock.Mock(return_value=mock.Mock())
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    from constraints.llm_backend import RemoteLLMBackend
    assert isinstance(
        get_constrained_backend(task="intent"), DeterministicFallbackBackend
    )
    assert isinstance(
        get_constrained_backend(task="shadow"), DeterministicFallbackBackend
    )
    # `recipe` is NOT in the disable list — uses anthropic
    assert isinstance(get_constrained_backend(task="recipe"), RemoteLLMBackend)


def test_disable_for_unknown_task_ignored(monkeypatch):
    """Typos in ASOE_LLM_DISABLE_FOR must not silently disable the
    wrong task or crash. Unknown tokens are dropped."""
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "fallback")
    monkeypatch.setenv("ASOE_LLM_DISABLE_FOR", "intent, made_up_task, shadow")
    # 'made_up_task' is dropped; intent and shadow disabled (no
    # behavior change since the global is fallback already).
    backend = get_constrained_backend(task="recipe")
    assert isinstance(backend, DeterministicFallbackBackend)


# ---------------------------------------------------------------------------
# USE_OUTLINES_BACKEND legacy short-circuit
# ---------------------------------------------------------------------------


def test_use_outlines_legacy_path(monkeypatch):
    """Legacy USE_OUTLINES_BACKEND=1 still resolves to outlines, but
    the actual import will fail in CI (no outlines installed) and
    the router falls closed to fallback. Test the fallthrough."""
    monkeypatch.setenv("USE_OUTLINES_BACKEND", "1")
    monkeypatch.delenv("ASOE_LLM_PROVIDER", raising=False)
    backend = get_constrained_backend()
    # outlines isn't installed in the test env — fallback applied.
    assert isinstance(backend, DeterministicFallbackBackend)
