from __future__ import annotations

# Provider factory coverage.
#
# Verifies:
#   - PROVIDER_FACTORIES has the four expected provider keys
#   - build_provider_client(provider) raises UnknownProvider on
#     a typo / unregistered key
#   - build_provider_client('anthropic') uses the Anthropic factory
#     (with a stubbed SDK)
#   - Stub providers raise NotImplementedError, surfaced as-is so
#     the router can distinguish "config error" from "provider call
#     failed"

import sys
from unittest import mock

import pytest

from llm.provider_factory import (
    PROVIDER_FACTORIES,
    UnknownProvider,
    build_provider_client,
)


def test_registry_has_all_expected_providers() -> None:
    assert set(PROVIDER_FACTORIES.keys()) == {
        "anthropic",
        "openai",
        "google",
        "ollama",
        "huggingface",
    }


def test_build_unknown_provider_raises() -> None:
    with pytest.raises(UnknownProvider, match="bogus"):
        build_provider_client("bogus")


def test_build_anthropic_uses_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ASOE_ENV", "sandbox")

    fake = mock.Mock()
    fake.Anthropic = mock.Mock(return_value=mock.Mock())
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    client = build_provider_client("anthropic")
    assert client.provider_name == "anthropic"
    fake.Anthropic.assert_called_once()


def test_build_anthropic_propagates_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        build_provider_client("anthropic")


def test_build_openai_propagates_not_implemented(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://my-azure.example/")
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    with pytest.raises(NotImplementedError, match="V1 stub"):
        build_provider_client("openai")


def test_build_google_propagates_not_implemented(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_PROJECT_ID", "my-proj")
    monkeypatch.setenv("GOOGLE_REGION", "us-east5")
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    with pytest.raises(NotImplementedError, match="V1 stub"):
        build_provider_client("google")


def test_build_ollama_propagates_not_implemented(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    with pytest.raises(NotImplementedError, match="V1 stub"):
        build_provider_client("ollama")


def test_build_huggingface_propagates_not_implemented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HUGGINGFACE_API_KEY", "hf_test")
    monkeypatch.setenv("HUGGINGFACE_MODEL", "Qwen/Qwen2.5-32B-Instruct")
    monkeypatch.setenv(
        "HUGGINGFACE_BASE_URL",
        "https://my-endpoint.endpoints.huggingface.cloud",
    )
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    with pytest.raises(NotImplementedError, match="V1 stub"):
        build_provider_client("huggingface")
