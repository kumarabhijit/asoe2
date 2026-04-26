from __future__ import annotations

# Coverage for llm/openai_client.py — full implementation.
#
# Network-free: openai.OpenAI / openai.AzureOpenAI are stubbed via
# sys.modules. Tests exercise:
#   - from_config: api_version=None → OpenAI(); api_version=set →
#     AzureOpenAI() with azure_endpoint + api_version + azure_deployment
#   - Production egress blocks public api.openai.com (all variants)
#   - Production allows Azure endpoints / private base URLs
#   - Kill switch blocks construction
#   - Missing OPENAI_API_KEY raises ValueError
#   - Missing SDK raises ImportError with install hint
#   - call_with_tool maps system blocks → concatenated system msg
#   - JSON-string arguments parsed (OpenAI shape)
#   - Dict arguments parsed (vLLM-compat path)
#   - Token usage from prompt_tokens / completion_tokens
#   - cached_tokens from prompt_tokens_details populates
#     cache_read_input_tokens (and is subtracted from input_tokens)
#   - Missing tool_calls → ProviderError(kind='schema_mismatch')
#   - Exception classification: 429/5xx/401/400/timeout/connection

import builtins
import sys
from unittest import mock

import pytest

from llm.anthropic_client import RemoteLLMConfig
from llm.openai_client import OpenAIProviderClient
from llm.provider_protocol import CacheControl, ProviderError, SystemBlock


# ---------------------------------------------------------------------------
# from_config — Azure vs OpenAI selection
# ---------------------------------------------------------------------------


def test_from_config_picks_openai_when_no_api_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    captured: dict[str, object] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.flavour = "openai"

    fake = mock.Mock()
    fake.OpenAI = FakeOpenAI
    fake.AzureOpenAI = mock.Mock(side_effect=AssertionError("should not be called"))
    monkeypatch.setitem(sys.modules, "openai", fake)

    cfg = RemoteLLMConfig(
        api_key="sk-test",
        base_url="https://my-vllm.example/v1",
        model_id="Qwen/Qwen2.5-32B-Instruct",
    )
    client = OpenAIProviderClient.from_config(cfg, asoe_env="sandbox")
    assert client.provider_name == "openai"
    assert client._is_azure is False  # type: ignore[attr-defined]
    assert captured["api_key"] == "sk-test"
    assert captured["base_url"] == "https://my-vllm.example/v1"
    assert captured["timeout"] == 30.0
    assert captured["max_retries"] == 2
    # No Azure-only fields
    assert "api_version" not in captured
    assert "azure_endpoint" not in captured


def test_from_config_picks_azure_when_api_version_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    captured: dict[str, object] = {}

    class FakeAzureOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake = mock.Mock()
    fake.OpenAI = mock.Mock(side_effect=AssertionError("should not be called"))
    fake.AzureOpenAI = FakeAzureOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake)

    cfg = RemoteLLMConfig(
        api_key="azure-key",
        base_url="https://myresource.openai.azure.com",
        api_version="2024-02-01",
        deployment_name="asoe-prod-gpt4o",
        model_id="gpt-4o",
    )
    client = OpenAIProviderClient.from_config(cfg, asoe_env="sandbox")
    assert client._is_azure is True  # type: ignore[attr-defined]
    # Azure passes endpoint + api_version (NOT base_url)
    assert captured["azure_endpoint"] == "https://myresource.openai.azure.com"
    assert captured["api_version"] == "2024-02-01"
    assert captured["azure_deployment"] == "asoe-prod-gpt4o"
    assert "base_url" not in captured
    # When deployment is set, the call-time `model` argument uses it,
    # not the underlying model_id.
    assert client._model_id == "asoe-prod-gpt4o"  # type: ignore[attr-defined]


def test_from_config_azure_falls_back_to_model_id_when_no_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    fake = mock.Mock()
    fake.AzureOpenAI = mock.Mock(return_value=mock.Mock())
    monkeypatch.setitem(sys.modules, "openai", fake)

    cfg = RemoteLLMConfig(
        api_key="azure-key",
        base_url="https://myresource.openai.azure.com",
        api_version="2024-02-01",
        model_id="gpt-4o",
    )
    client = OpenAIProviderClient.from_config(cfg, asoe_env="sandbox")
    assert client._model_id == "gpt-4o"  # type: ignore[attr-defined]


def test_from_config_azure_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """api_version without base_url is a misconfiguration — Azure
    endpoint is required."""
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    fake = mock.Mock()
    monkeypatch.setitem(sys.modules, "openai", fake)

    cfg = RemoteLLMConfig(
        api_key="x",
        api_version="2024-02-01",
        base_url=None,
    )
    with pytest.raises(ValueError, match="OPENAI_BASE_URL"):
        OpenAIProviderClient.from_config(cfg, asoe_env="sandbox")


# ---------------------------------------------------------------------------
# Production egress gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blocked",
    [
        None,
        "https://api.openai.com",
        "https://api.openai.com/",
        "https://api.openai.com/v1",
        "https://api.openai.com/v1/",
        "api.openai.com",
    ],
)
def test_production_blocks_public_openai(blocked: str | None) -> None:
    cfg = RemoteLLMConfig(api_key="sk-test", base_url=blocked)
    with pytest.raises(RuntimeError, match="api.openai.com"):
        OpenAIProviderClient.from_config(cfg, asoe_env="production")


def test_production_allows_azure_private_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    fake = mock.Mock()
    fake.AzureOpenAI = mock.Mock(return_value=mock.Mock())
    monkeypatch.setitem(sys.modules, "openai", fake)

    cfg = RemoteLLMConfig(
        api_key="azure-key",
        base_url="https://myresource.openai.azure.com",
        api_version="2024-02-01",
    )
    client = OpenAIProviderClient.from_config(cfg, asoe_env="production")
    assert client.provider_name == "openai"


def test_production_allows_self_hosted_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    """A self-hosted vLLM/TGI cluster URL is operator-controlled and
    permitted in production."""
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    fake = mock.Mock()
    fake.OpenAI = mock.Mock(return_value=mock.Mock())
    monkeypatch.setitem(sys.modules, "openai", fake)

    cfg = RemoteLLMConfig(
        api_key="placeholder",
        base_url="https://vllm.private.example/v1",
    )
    client = OpenAIProviderClient.from_config(cfg, asoe_env="production")
    assert client.provider_name == "openai"


# ---------------------------------------------------------------------------
# Auth / kill switch / missing dep
# ---------------------------------------------------------------------------


def test_missing_api_key_raises() -> None:
    cfg = RemoteLLMConfig(api_key=None)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIProviderClient.from_config(cfg, asoe_env="sandbox")


def test_kill_switch_blocks_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "1")
    cfg = RemoteLLMConfig(api_key="sk-test")
    with pytest.raises(RuntimeError, match="ASOE_KILL_SWITCH"):
        OpenAIProviderClient.from_config(cfg, asoe_env="sandbox")


def test_missing_dep_raises_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")
    monkeypatch.delitem(sys.modules, "openai", raising=False)

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "openai":
            raise ImportError("No module named 'openai'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    cfg = RemoteLLMConfig(api_key="sk-test")
    with pytest.raises(ImportError, match=r"asoe\[openai\]"):
        OpenAIProviderClient.from_config(cfg, asoe_env="sandbox")


def test_extra_headers_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    captured: dict[str, object] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake = mock.Mock()
    fake.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake)

    cfg = RemoteLLMConfig(
        api_key="sk-test",
        extra_headers=(("x-tenant-id", "asoe"), ("x-request-source", "asoe-core")),
    )
    OpenAIProviderClient.from_config(cfg, asoe_env="sandbox")
    headers = captured["default_headers"]
    assert headers["x-tenant-id"] == "asoe"
    assert headers["x-request-source"] == "asoe-core"


# ---------------------------------------------------------------------------
# call_with_tool — happy paths
# ---------------------------------------------------------------------------


def _openai_response(
    *,
    tool_name: str,
    args,
    prompt_tokens: int = 120,
    completion_tokens: int = 15,
    cached_tokens: int | None = None,
    finish_reason: str = "stop",
):
    """Build a fake OpenAI chat-completion response with attribute
    access (matching the typed pydantic-like objects the SDK returns)."""
    function_obj = mock.Mock()
    function_obj.name = tool_name
    function_obj.arguments = args

    tool_call = mock.Mock()
    tool_call.id = "call_test"
    tool_call.type = "function"
    tool_call.function = function_obj

    message = mock.Mock()
    message.role = "assistant"
    message.content = None
    message.tool_calls = [tool_call]

    choice = mock.Mock()
    choice.finish_reason = finish_reason
    choice.index = 0
    choice.message = message

    usage = mock.Mock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.total_tokens = prompt_tokens + completion_tokens
    if cached_tokens is not None:
        details = mock.Mock()
        details.cached_tokens = cached_tokens
        usage.prompt_tokens_details = details
    else:
        usage.prompt_tokens_details = None

    response = mock.Mock()
    response.choices = [choice]
    response.usage = usage
    response.id = "chatcmpl-test123"
    response._request_id = "req_OpenAI_xyz"
    response.model = "gpt-4o-2024-08-06"
    return response


def test_call_with_tool_parses_json_string_arguments() -> None:
    fake_sdk = mock.Mock()
    fake_sdk.chat.completions.create.return_value = _openai_response(
        tool_name="classify_intent",
        args='{"intent": "DUPLICATE_PO", "confidence": 0.93}',
    )

    client = OpenAIProviderClient(sdk_client=fake_sdk, model_id="gpt-4o")
    result = client.call_with_tool(
        system=[
            SystemBlock(text="big skill content", cache=CacheControl(enabled=True)),
            SystemBlock(text="more guidance", cache=CacheControl()),
        ],
        user_message="event payload",
        tool_name="classify_intent",
        tool_description="Classify the intent.",
        tool_input_schema={"type": "object", "properties": {}},
        max_tokens=100,
    )

    assert result.tool_name == "classify_intent"
    assert result.arguments == {"intent": "DUPLICATE_PO", "confidence": 0.93}
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 15
    assert result.usage.cache_read_input_tokens == 0
    assert result.model_id == "gpt-4o-2024-08-06"
    assert result.stop_reason == "stop"
    # Newer SDK exposes _request_id; preferred over chat-completion id.
    assert result.request_id == "req_OpenAI_xyz"

    # Inspect the call args
    fake_sdk.chat.completions.create.assert_called_once()
    kwargs = fake_sdk.chat.completions.create.call_args.kwargs
    msgs = kwargs["messages"]
    assert msgs[0]["role"] == "system"
    assert "big skill content" in msgs[0]["content"]
    assert "more guidance" in msgs[0]["content"]
    assert msgs[1]["role"] == "user"
    assert kwargs["tools"][0]["type"] == "function"
    assert kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "classify_intent"},
    }
    assert kwargs["max_tokens"] == 100
    assert kwargs["temperature"] == 0.0


def test_cached_tokens_populate_cache_read_input_tokens() -> None:
    """OpenAI's automatic prompt caching surfaces as
    usage.prompt_tokens_details.cached_tokens. We populate
    cache_read_input_tokens AND subtract from input_tokens to avoid
    double-charging."""
    fake_sdk = mock.Mock()
    fake_sdk.chat.completions.create.return_value = _openai_response(
        tool_name="classify_intent",
        args='{"intent": "DUPLICATE_PO", "confidence": 0.9}',
        prompt_tokens=6500,  # total prompt tokens
        cached_tokens=6000,  # of which 6000 are cached
    )

    client = OpenAIProviderClient(sdk_client=fake_sdk, model_id="gpt-4o")
    result = client.call_with_tool(
        system=[],
        user_message="u",
        tool_name="classify_intent",
        tool_description="d",
        tool_input_schema={"type": "object"},
    )
    # input_tokens counts the *uncached* portion only
    assert result.usage.input_tokens == 500
    assert result.usage.cache_read_input_tokens == 6000


def test_falls_back_to_id_when_no_request_id_attribute() -> None:
    """Older SDK versions / OpenAI-compat servers might not expose
    _request_id. Fall back to `id` (chat-completion id) — still
    useful for support."""
    fake_sdk = mock.Mock()
    response = _openai_response(
        tool_name="classify_intent",
        args='{"intent": "DUPLICATE_PO", "confidence": 0.9}',
    )
    response._request_id = None
    fake_sdk.chat.completions.create.return_value = response

    client = OpenAIProviderClient(sdk_client=fake_sdk, model_id="gpt-4o")
    result = client.call_with_tool(
        system=[], user_message="u",
        tool_name="classify_intent", tool_description="d",
        tool_input_schema={"type": "object"},
    )
    assert result.request_id == "chatcmpl-test123"


def test_dict_arguments_path() -> None:
    """Some OpenAI-compatible servers (vLLM with certain configs)
    return arguments as already-parsed dicts. Handle that path too."""
    fake_sdk = mock.Mock()
    fake_sdk.chat.completions.create.return_value = _openai_response(
        tool_name="classify_intent",
        args={"intent": "DUPLICATE_PO", "confidence": 0.9},
    )
    client = OpenAIProviderClient(sdk_client=fake_sdk, model_id="x")
    result = client.call_with_tool(
        system=[], user_message="u",
        tool_name="classify_intent", tool_description="d",
        tool_input_schema={"type": "object"},
    )
    assert result.arguments == {"intent": "DUPLICATE_PO", "confidence": 0.9}


def test_handles_missing_usage_field() -> None:
    fake_sdk = mock.Mock()
    response = _openai_response(
        tool_name="classify_intent",
        args='{"intent": "DUPLICATE_PO", "confidence": 0.9}',
    )
    response.usage = None
    fake_sdk.chat.completions.create.return_value = response

    client = OpenAIProviderClient(sdk_client=fake_sdk, model_id="x")
    result = client.call_with_tool(
        system=[], user_message="u",
        tool_name="classify_intent", tool_description="d",
        tool_input_schema={"type": "object"},
    )
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0


# ---------------------------------------------------------------------------
# call_with_tool — error paths
# ---------------------------------------------------------------------------


def test_no_choices_raises_schema_mismatch() -> None:
    fake_sdk = mock.Mock()
    response = mock.Mock()
    response.choices = []
    response._request_id = "req_x"
    response.id = "chatcmpl-x"
    fake_sdk.chat.completions.create.return_value = response

    client = OpenAIProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "schema_mismatch"
    assert ei.value.request_id == "req_x"


def test_missing_tool_call_raises_schema_mismatch() -> None:
    """finish_reason='length' — model hit max_tokens before
    completing the tool call."""
    fake_sdk = mock.Mock()
    response = _openai_response(
        tool_name="classify_intent",
        args='{"intent": "DUPLICATE_PO", "confidence": 0.9}',
        finish_reason="length",
    )
    # Wipe the tool_calls to simulate truncation
    response.choices[0].message.tool_calls = []
    fake_sdk.chat.completions.create.return_value = response

    client = OpenAIProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="classify_intent", tool_description="d",
            tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "schema_mismatch"


def test_invalid_json_arguments_raises_schema_mismatch() -> None:
    fake_sdk = mock.Mock()
    fake_sdk.chat.completions.create.return_value = _openai_response(
        tool_name="classify_intent",
        args="{not valid json",
    )
    client = OpenAIProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="classify_intent", tool_description="d",
            tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "schema_mismatch"


def test_unknown_arg_type_raises_schema_mismatch() -> None:
    fake_sdk = mock.Mock()
    fake_sdk.chat.completions.create.return_value = _openai_response(
        tool_name="classify_intent",
        args=42,  # neither dict nor str
    )
    client = OpenAIProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="classify_intent", tool_description="d",
            tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "schema_mismatch"


# ---------------------------------------------------------------------------
# Exception classification
# ---------------------------------------------------------------------------


def _make_exception(name: str, *, status_code: int | None = None):
    """Construct a fake openai.<Exception> with the right name + status."""
    cls = type(name, (Exception,), {})
    cls.__name__ = name
    exc = cls(f"{name} fired")
    if status_code is not None:
        exc.status_code = status_code  # type: ignore[attr-defined]
        exc.response = mock.Mock(status_code=status_code)
    return exc


def test_429_classifies_as_rate_limit() -> None:
    fake_sdk = mock.Mock()
    fake_sdk.chat.completions.create.side_effect = _make_exception(
        "RateLimitError", status_code=429
    )
    client = OpenAIProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "rate_limit"
    assert ei.value.retryable is True
    assert ei.value.status_code == 429


def test_503_classifies_as_server_error() -> None:
    fake_sdk = mock.Mock()
    fake_sdk.chat.completions.create.side_effect = _make_exception(
        "InternalServerError", status_code=503
    )
    client = OpenAIProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "server_error"
    assert ei.value.retryable is True


def test_401_classifies_as_auth() -> None:
    fake_sdk = mock.Mock()
    fake_sdk.chat.completions.create.side_effect = _make_exception(
        "AuthenticationError", status_code=401
    )
    client = OpenAIProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "auth"
    assert ei.value.retryable is False


def test_400_classifies_as_schema_mismatch() -> None:
    fake_sdk = mock.Mock()
    fake_sdk.chat.completions.create.side_effect = _make_exception(
        "BadRequestError", status_code=400
    )
    client = OpenAIProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "schema_mismatch"
    assert ei.value.retryable is False


def test_timeout_classifies_as_timeout() -> None:
    fake_sdk = mock.Mock()
    fake_sdk.chat.completions.create.side_effect = _make_exception("APITimeoutError")
    client = OpenAIProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "timeout"
    assert ei.value.retryable is True


def test_connection_error_classifies_as_connection() -> None:
    fake_sdk = mock.Mock()
    fake_sdk.chat.completions.create.side_effect = _make_exception("APIConnectionError")
    client = OpenAIProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "connection"
    assert ei.value.retryable is True


def test_unknown_exception_classifies_as_unknown() -> None:
    fake_sdk = mock.Mock()
    fake_sdk.chat.completions.create.side_effect = ValueError("totally unexpected")
    client = OpenAIProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "unknown"
    assert ei.value.retryable is False
