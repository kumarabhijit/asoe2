from __future__ import annotations

# Stub-provider coverage.
#
# In V1, only `anthropic` is fully wired. `openai`, `google`, `ollama`
# satisfy the LLMProviderClient Protocol but their `from_config()` /
# `call_with_tool()` raise NotImplementedError / ProviderError. The
# router catches and falls through to deterministic.
#
# These tests pin that behavior so:
#   1. Adding a new provider doesn't accidentally drop the prod-egress
#      check.
#   2. The router's fallthrough contract stays explicit.
#   3. Once a stub is implemented, the test file is updated rather
#      than silently passing.

import pytest

from llm import RemoteLLMConfig
from llm.google_client import GoogleProviderClient
from llm.huggingface_client import HuggingFaceProviderClient
from llm.ollama_client import OllamaProviderClient
from llm.openai_client import OpenAIProviderClient
from llm.provider_protocol import LLMProviderClient, ProviderError, SystemBlock


@pytest.mark.parametrize(
    "client_cls",
    [
        OpenAIProviderClient,
        GoogleProviderClient,
        OllamaProviderClient,
        HuggingFaceProviderClient,
    ],
)
def test_stub_satisfies_protocol(client_cls) -> None:
    """Every stub must structurally satisfy the Protocol so the
    factory can return one without a TypeError later."""
    # The class itself implements the Protocol via methods; we
    # check the attribute surface explicitly because runtime_checkable
    # Protocol with class objects is loose.
    assert client_cls.provider_name in {"openai", "google", "ollama", "huggingface"}
    assert callable(getattr(client_cls, "from_config", None))
    assert callable(getattr(client_cls, "call_with_tool", None))


def test_openai_stub_raises_not_implemented_in_sandbox() -> None:
    cfg = RemoteLLMConfig(api_key="x", base_url="https://my-azure-openai.example/")
    with pytest.raises(NotImplementedError, match="V1 stub"):
        OpenAIProviderClient.from_config(cfg, asoe_env="sandbox")


def test_google_stub_raises_not_implemented_in_sandbox() -> None:
    cfg = RemoteLLMConfig(api_key="x", project_id="p", region="us-east5")
    with pytest.raises(NotImplementedError, match="V1 stub"):
        GoogleProviderClient.from_config(cfg, asoe_env="sandbox")


def test_ollama_stub_raises_not_implemented_in_sandbox() -> None:
    cfg = RemoteLLMConfig(api_key=None, base_url="http://localhost:11434/v1")
    with pytest.raises(NotImplementedError, match="V1 stub"):
        OllamaProviderClient.from_config(cfg, asoe_env="sandbox")


def test_huggingface_stub_raises_not_implemented_in_sandbox() -> None:
    cfg = RemoteLLMConfig(
        api_key="hf_test",
        model_id="Qwen/Qwen2.5-32B-Instruct",
        base_url="https://my-endpoint.endpoints.huggingface.cloud",
    )
    with pytest.raises(NotImplementedError, match="V1 stub"):
        HuggingFaceProviderClient.from_config(cfg, asoe_env="sandbox")


# ---------------------------------------------------------------------------
# Production egress gates (must fire BEFORE the NotImplementedError)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blocked",
    [None, "https://api.openai.com", "https://api.openai.com/", "api.openai.com",
     "https://api.openai.com/v1"],
)
def test_openai_production_blocks_public(blocked: str | None) -> None:
    cfg = RemoteLLMConfig(api_key="x", base_url=blocked)
    with pytest.raises(RuntimeError, match="api.openai.com"):
        OpenAIProviderClient.from_config(cfg, asoe_env="production")


@pytest.mark.parametrize(
    "blocked",
    [
        None,
        "https://generativelanguage.googleapis.com",
        "https://generativelanguage.googleapis.com/",
    ],
)
def test_google_production_requires_vertex(blocked: str | None) -> None:
    cfg = RemoteLLMConfig(api_key="x", base_url=blocked)
    with pytest.raises(RuntimeError, match="Vertex"):
        GoogleProviderClient.from_config(cfg, asoe_env="production")


@pytest.mark.parametrize(
    "blocked",
    [None, "https://ollama.com", "https://api.ollama.com",
     "ollama.com", "api.ollama.com"],
)
def test_ollama_production_blocks_public_cloud(blocked: str | None) -> None:
    cfg = RemoteLLMConfig(api_key="x", base_url=blocked)
    with pytest.raises(RuntimeError, match="Ollama"):
        OllamaProviderClient.from_config(cfg, asoe_env="production")


@pytest.mark.parametrize(
    "blocked",
    [
        None,
        "https://api-inference.huggingface.co",
        "https://api-inference.huggingface.co/",
        "api-inference.huggingface.co",
    ],
)
def test_huggingface_production_blocks_public_serverless(blocked: str | None) -> None:
    cfg = RemoteLLMConfig(api_key="hf_test", base_url=blocked)
    with pytest.raises(RuntimeError, match="HuggingFace"):
        HuggingFaceProviderClient.from_config(cfg, asoe_env="production")


def test_huggingface_production_allows_dedicated_endpoint() -> None:
    """Customer-owned Dedicated Inference Endpoints are permitted in
    production — the URL is operator-controlled."""
    cfg = RemoteLLMConfig(
        api_key="hf_test",
        base_url="https://my-endpoint.endpoints.huggingface.cloud",
    )
    # Past the prod-egress check it raises NotImplementedError (V1
    # stub), not RuntimeError. That's the success signal: the gate
    # didn't fire.
    with pytest.raises(NotImplementedError, match="V1 stub"):
        HuggingFaceProviderClient.from_config(cfg, asoe_env="production")


# ---------------------------------------------------------------------------
# call_with_tool always raises ProviderError (router catches this kind
# specifically and falls through to deterministic)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_cls",
    [
        OpenAIProviderClient,
        GoogleProviderClient,
        OllamaProviderClient,
        HuggingFaceProviderClient,
    ],
)
def test_stub_call_raises_provider_error(client_cls) -> None:
    """Direct stub instances raise ProviderError on call_with_tool —
    the router catches and routes to deterministic without crashing
    the graph."""
    inst = client_cls(sdk_client=None, model_id="x")
    with pytest.raises(ProviderError) as ei:
        inst.call_with_tool(
            system=[SystemBlock(text="x")],
            user_message="u",
            tool_name="t",
            tool_description="d",
            tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "unknown"
    assert ei.value.retryable is False
