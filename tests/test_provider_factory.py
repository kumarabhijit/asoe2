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


def test_build_openai_constructs_for_compatible_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI is fully implemented in V1 PR-1. Using a self-hosted
    OpenAI-compatible base URL builds the OpenAI() client class
    (NOT AzureOpenAI) because no api_version is set."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://my-vllm.example/v1")
    monkeypatch.delenv("OPENAI_API_VERSION", raising=False)
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    fake = mock.Mock()
    fake.OpenAI = mock.Mock(return_value=mock.Mock())
    fake.AzureOpenAI = mock.Mock(return_value=mock.Mock())
    monkeypatch.setitem(sys.modules, "openai", fake)

    client = build_provider_client("openai")
    assert client.provider_name == "openai"
    fake.OpenAI.assert_called_once()
    fake.AzureOpenAI.assert_not_called()


def test_build_openai_constructs_for_azure(monkeypatch: pytest.MonkeyPatch) -> None:
    """api_version presence selects the AzureOpenAI client class."""
    monkeypatch.setenv("OPENAI_API_KEY", "azure-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://myresource.openai.azure.com")
    monkeypatch.setenv("OPENAI_API_VERSION", "2024-02-01")
    monkeypatch.setenv("OPENAI_DEPLOYMENT", "asoe-prod-gpt4o")
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    fake = mock.Mock()
    fake.OpenAI = mock.Mock(return_value=mock.Mock())
    fake.AzureOpenAI = mock.Mock(return_value=mock.Mock())
    monkeypatch.setitem(sys.modules, "openai", fake)

    client = build_provider_client("openai")
    assert client.provider_name == "openai"
    fake.AzureOpenAI.assert_called_once()
    fake.OpenAI.assert_not_called()


def test_build_openai_missing_key_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        build_provider_client("openai")


def test_build_google_propagates_not_implemented(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_PROJECT_ID", "my-proj")
    monkeypatch.setenv("GOOGLE_REGION", "us-east5")
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    with pytest.raises(NotImplementedError, match="V1 stub"):
        build_provider_client("google")


def test_build_ollama_constructs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ollama is fully implemented in V1 PR-1; the factory must
    construct an OllamaProviderClient when the SDK is available."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    fake = mock.Mock()
    fake.Client = mock.Mock(return_value=mock.Mock())
    monkeypatch.setitem(sys.modules, "ollama", fake)

    client = build_provider_client("ollama")
    assert client.provider_name == "ollama"
    fake.Client.assert_called_once()


def test_build_huggingface_constructs(monkeypatch: pytest.MonkeyPatch) -> None:
    """HuggingFace is fully implemented in V1 PR-1."""
    monkeypatch.setenv("HUGGINGFACE_API_KEY", "hf_test")
    monkeypatch.setenv("HUGGINGFACE_MODEL", "Qwen/Qwen2.5-32B-Instruct")
    monkeypatch.setenv(
        "HUGGINGFACE_BASE_URL",
        "https://my-endpoint.endpoints.huggingface.cloud",
    )
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    fake = mock.Mock()
    fake.InferenceClient = mock.Mock(return_value=mock.Mock())
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)

    client = build_provider_client("huggingface")
    assert client.provider_name == "huggingface"
    fake.InferenceClient.assert_called_once()


def test_build_huggingface_missing_key_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUGGINGFACE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="HUGGINGFACE_API_KEY"):
        build_provider_client("huggingface")
