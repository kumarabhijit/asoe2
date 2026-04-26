from __future__ import annotations

# Coverage for llm/anthropic_client.py
#
# Verifies:
#   - RemoteLLMConfig.from_env reads every supported env var
#   - Required ANTHROPIC_API_KEY raises ValueError when absent
#   - ASOE_ENV=production rejects public Anthropic egress
#   - build_client respects the kill switch (no TCP open when active)
#   - build_client raises ImportError with an actionable message when
#     the optional `anthropic` dep is missing — but only attempts import
#     when called (the module itself stays importable without the dep)

import builtins
import os
import sys
from unittest import mock

import pytest

from llm.anthropic_client import (
    ProductionEgressBlocked,
    RemoteLLMConfig,
    build_client,
)


# ---------------------------------------------------------------------------
# RemoteLLMConfig.from_env
# ---------------------------------------------------------------------------


def test_from_env_minimum_required_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-abc")
    for key in (
        "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_AZURE_DEPLOYMENT",
        "ANTHROPIC_API_VERSION", "ANTHROPIC_REGION", "ANTHROPIC_TIMEOUT_S",
        "ANTHROPIC_MAX_RETRIES", "ANTHROPIC_BETA_HEADERS",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = RemoteLLMConfig.from_env()
    assert cfg.api_key == "sk-test-abc"
    assert cfg.base_url is None
    assert cfg.model_id == "claude-sonnet-4-6"  # LLM_DEFAULT_MODEL_ID
    assert cfg.azure_deployment_name is None
    assert cfg.api_version is None
    assert cfg.region is None
    assert cfg.timeout_s == 30.0  # LLM_CALL_TIMEOUT_S
    assert cfg.max_retries == 2
    assert cfg.beta_headers == ()


def test_from_env_all_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://foundry.example/anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-4-7")
    monkeypatch.setenv("ANTHROPIC_AZURE_DEPLOYMENT", "asoe-prod-claude")
    monkeypatch.setenv("ANTHROPIC_API_VERSION", "2023-06-01")
    monkeypatch.setenv("ANTHROPIC_REGION", "eastus")
    monkeypatch.setenv("ANTHROPIC_TIMEOUT_S", "45.0")
    monkeypatch.setenv("ANTHROPIC_MAX_RETRIES", "3")
    monkeypatch.setenv(
        "ANTHROPIC_BETA_HEADERS",
        "task-budgets-2026-03-13, compact-2026-01-12",
    )

    cfg = RemoteLLMConfig.from_env()
    assert cfg.base_url == "https://foundry.example/anthropic"
    assert cfg.model_id == "claude-opus-4-7"
    assert cfg.azure_deployment_name == "asoe-prod-claude"
    assert cfg.api_version == "2023-06-01"
    assert cfg.region == "eastus"
    assert cfg.timeout_s == 45.0
    assert cfg.max_retries == 3
    assert cfg.beta_headers == (
        "task-budgets-2026-03-13",
        "compact-2026-01-12",
    )


def test_from_env_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        RemoteLLMConfig.from_env()


def test_from_env_blank_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        RemoteLLMConfig.from_env()


def test_from_env_is_frozen() -> None:
    cfg = RemoteLLMConfig(api_key="x")
    with pytest.raises(Exception):  # pydantic ValidationError on frozen
        cfg.api_key = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Production egress gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blocked",
    [None, "https://api.anthropic.com", "https://api.anthropic.com/", "api.anthropic.com"],
)
def test_production_blocks_public_anthropic(blocked: str | None) -> None:
    cfg = RemoteLLMConfig(api_key="x", base_url=blocked)
    with pytest.raises(ProductionEgressBlocked):
        cfg.assert_production_egress_allowed("production")


def test_production_allows_foundry_endpoint() -> None:
    cfg = RemoteLLMConfig(
        api_key="x", base_url="https://foundry.private.example/anthropic"
    )
    # Should not raise
    cfg.assert_production_egress_allowed("production")


def test_sandbox_allows_public_anthropic() -> None:
    cfg = RemoteLLMConfig(api_key="x", base_url=None)
    # Sandbox: anything goes
    cfg.assert_production_egress_allowed("sandbox")


# ---------------------------------------------------------------------------
# build_client kill-switch + missing dep
# ---------------------------------------------------------------------------


def test_build_client_blocked_by_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "1")
    cfg = RemoteLLMConfig(api_key="x")
    with pytest.raises(RuntimeError, match="ASOE_KILL_SWITCH"):
        build_client(cfg)


def test_build_client_missing_dep_raises_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the anthropic package isn't installed, build_client must
    raise ImportError with an install hint — not a NameError or
    AttributeError that hides the root cause."""
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    # Force `import anthropic` inside build_client to fail.
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "anthropic":
            raise ImportError("No module named 'anthropic'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    cfg = RemoteLLMConfig(api_key="x")
    with pytest.raises(ImportError, match=r"asoe\[anthropic\]"):
        build_client(cfg)


def test_build_client_constructs_when_dep_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub the anthropic module and assert build_client passes the
    expected kwargs. We construct a minimal fake to keep the test
    network-free."""
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    captured: dict[str, object] = {}

    class FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = mock.Mock()
    fake_module.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    cfg = RemoteLLMConfig(
        api_key="sk-test",
        base_url="https://foundry.example/anthropic",
        api_version="2023-06-01",
        azure_deployment_name="asoe-prod-claude",
        timeout_s=20.0,
        max_retries=1,
    )
    client = build_client(cfg)

    assert isinstance(client, FakeAnthropic)
    assert captured["api_key"] == "sk-test"
    assert captured["timeout"] == 20.0
    assert captured["max_retries"] == 1
    assert captured["base_url"] == "https://foundry.example/anthropic"
    headers = captured["default_headers"]
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["x-azure-deployment"] == "asoe-prod-claude"


def test_build_client_omits_optional_headers_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    captured: dict[str, object] = {}

    class FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = mock.Mock()
    fake_module.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    cfg = RemoteLLMConfig(api_key="sk-test")
    build_client(cfg)

    # When base_url and api_version and deployment are all None, no
    # default_headers should be passed at all.
    assert "default_headers" not in captured
    assert "base_url" not in captured
