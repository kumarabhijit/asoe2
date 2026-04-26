from __future__ import annotations

# Coverage for llm/huggingface_client.py — full implementation.
#
# Network-free: huggingface_hub.InferenceClient is stubbed via
# sys.modules. Tests exercise:
#   - from_config requires HUGGINGFACE_API_KEY
#   - Production blocks public Serverless Inference API
#   - Production allows Dedicated Inference Endpoint URLs
#   - Kill switch blocks construction
#   - Missing SDK raises ImportError with install hint
#   - InferenceClient is built with `base_url` (Dedicated) OR
#     `model` (Serverless), never both incorrectly mixed
#   - call_with_tool maps system blocks → concatenated system msg
#   - JSON-string arguments parsed (HF returns OpenAI-compatible
#     shape with arguments as a string)
#   - Token usage from usage.{prompt_tokens, completion_tokens}
#   - Missing tool_calls → ProviderError(kind='schema_mismatch')
#   - Exception classification: 429→rate_limit, 5xx→server_error,
#     timeout→timeout, network→connection

import builtins
import sys
from unittest import mock

import pytest

from llm.anthropic_client import RemoteLLMConfig
from llm.huggingface_client import HuggingFaceProviderClient
from llm.provider_protocol import CacheControl, ProviderError, SystemBlock


# ---------------------------------------------------------------------------
# from_config — auth + production-egress + kill switch
# ---------------------------------------------------------------------------


def test_from_config_requires_api_key() -> None:
    cfg = RemoteLLMConfig(api_key=None, base_url="https://my.endpoints.huggingface.cloud")
    with pytest.raises(ValueError, match="HUGGINGFACE_API_KEY"):
        HuggingFaceProviderClient.from_config(cfg, asoe_env="sandbox")


@pytest.mark.parametrize(
    "blocked",
    [
        None,
        "https://api-inference.huggingface.co",
        "https://api-inference.huggingface.co/",
        "api-inference.huggingface.co",
    ],
)
def test_from_config_blocks_public_serverless_in_production(blocked: str | None) -> None:
    cfg = RemoteLLMConfig(api_key="hf_test", base_url=blocked)
    with pytest.raises(RuntimeError, match="HuggingFace"):
        HuggingFaceProviderClient.from_config(cfg, asoe_env="production")


def test_from_config_allows_dedicated_endpoint_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    captured: dict[str, object] = {}

    class FakeInferenceClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake = mock.Mock()
    fake.InferenceClient = FakeInferenceClient
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)

    cfg = RemoteLLMConfig(
        api_key="hf_test",
        base_url="https://my.endpoints.huggingface.cloud",
        model_id="Qwen/Qwen2.5-32B-Instruct",
    )
    client = HuggingFaceProviderClient.from_config(cfg, asoe_env="production")
    assert client.provider_name == "huggingface"
    assert captured["token"] == "hf_test"
    assert captured["base_url"] == "https://my.endpoints.huggingface.cloud"
    # When base_url is set, model is NOT passed (the endpoint is the model)
    assert "model" not in captured


def test_from_config_uses_model_when_no_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serverless path: no base_url → InferenceClient routes by
    model id."""
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    captured: dict[str, object] = {}

    class FakeInferenceClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake = mock.Mock()
    fake.InferenceClient = FakeInferenceClient
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)

    cfg = RemoteLLMConfig(
        api_key="hf_test",
        model_id="Qwen/Qwen2.5-32B-Instruct",
    )
    HuggingFaceProviderClient.from_config(cfg, asoe_env="sandbox")
    assert captured["model"] == "Qwen/Qwen2.5-32B-Instruct"
    assert "base_url" not in captured


def test_from_config_attaches_extra_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    captured: dict[str, object] = {}

    class FakeInferenceClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake = mock.Mock()
    fake.InferenceClient = FakeInferenceClient
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)

    cfg = RemoteLLMConfig(
        api_key="hf_test",
        model_id="Qwen/Qwen2.5-32B-Instruct",
        extra_headers=(("x-tenant-id", "asoe-prod"),),
    )
    HuggingFaceProviderClient.from_config(cfg, asoe_env="sandbox")
    assert captured["headers"] == {"x-tenant-id": "asoe-prod"}


def test_from_config_blocked_by_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "1")
    cfg = RemoteLLMConfig(api_key="hf_test")
    with pytest.raises(RuntimeError, match="ASOE_KILL_SWITCH"):
        HuggingFaceProviderClient.from_config(cfg, asoe_env="sandbox")


def test_from_config_missing_dep_raises_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")
    monkeypatch.delitem(sys.modules, "huggingface_hub", raising=False)

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "huggingface_hub":
            raise ImportError("No module named 'huggingface_hub'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    cfg = RemoteLLMConfig(api_key="hf_test")
    with pytest.raises(ImportError, match=r"asoe\[huggingface\]"):
        HuggingFaceProviderClient.from_config(cfg, asoe_env="sandbox")


# ---------------------------------------------------------------------------
# call_with_tool — happy paths
# ---------------------------------------------------------------------------


def _hf_response(*, tool_name: str, args, prompt_tokens: int = 120, completion_tokens: int = 15):
    """Build a fake OpenAI-compatible chat_completion response with
    attribute access (matching the typed objects HF returns)."""
    function_obj = mock.Mock()
    function_obj.name = tool_name
    function_obj.arguments = args

    tool_call = mock.Mock()
    tool_call.id = "tc_abc"
    tool_call.type = "function"
    tool_call.function = function_obj

    message = mock.Mock()
    message.role = "assistant"
    message.content = ""
    message.tool_calls = [tool_call]

    choice = mock.Mock()
    choice.finish_reason = "stop"
    choice.index = 0
    choice.message = message

    usage = mock.Mock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.total_tokens = prompt_tokens + completion_tokens

    response = mock.Mock()
    response.choices = [choice]
    response.usage = usage
    response.id = "chatcmpl-test123"
    response.model = "Qwen/Qwen2.5-32B-Instruct"
    return response


def test_call_with_tool_parses_json_string_arguments() -> None:
    """HF Inference Endpoints return OpenAI-compatible shape with
    arguments as a JSON STRING (not dict)."""
    fake_sdk = mock.Mock()
    fake_sdk.chat_completion.return_value = _hf_response(
        tool_name="classify_intent",
        args='{"intent": "DUPLICATE_PO", "confidence": 0.92}',
    )

    client = HuggingFaceProviderClient(
        sdk_client=fake_sdk, model_id="Qwen/Qwen2.5-32B-Instruct"
    )
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
    assert result.arguments == {"intent": "DUPLICATE_PO", "confidence": 0.92}
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 15
    assert result.usage.cache_read_input_tokens == 0
    assert result.usage.cache_creation_input_tokens == 0
    assert result.model_id == "Qwen/Qwen2.5-32B-Instruct"
    assert result.stop_reason == "stop"
    assert result.request_id == "chatcmpl-test123"

    # Verify the call shape
    fake_sdk.chat_completion.assert_called_once()
    kwargs = fake_sdk.chat_completion.call_args.kwargs
    msgs = kwargs["messages"]
    # System blocks concatenated
    assert msgs[0]["role"] == "system"
    assert "big skill content" in msgs[0]["content"]
    assert "more guidance" in msgs[0]["content"]
    assert msgs[1]["role"] == "user"
    # Tool def OpenAI-style
    assert kwargs["tools"][0]["type"] == "function"
    assert kwargs["tools"][0]["function"]["name"] == "classify_intent"
    # Forced tool choice
    assert kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "classify_intent"},
    }
    assert kwargs["max_tokens"] == 100
    assert kwargs["temperature"] == 0.0


def test_call_with_tool_parses_dict_arguments() -> None:
    """Some HF model serializations may pre-parse args to a dict —
    handle that path too."""
    fake_sdk = mock.Mock()
    fake_sdk.chat_completion.return_value = _hf_response(
        tool_name="classify_intent",
        args={"intent": "DUPLICATE_PO", "confidence": 0.92},
    )

    client = HuggingFaceProviderClient(
        sdk_client=fake_sdk, model_id="Qwen/Qwen2.5-32B-Instruct"
    )
    result = client.call_with_tool(
        system=[],
        user_message="u",
        tool_name="classify_intent",
        tool_description="d",
        tool_input_schema={"type": "object"},
    )
    assert result.arguments == {"intent": "DUPLICATE_PO", "confidence": 0.92}


def test_call_with_tool_handles_missing_usage_field() -> None:
    """If the response object doesn't carry a usage block, token
    counts default to 0 — don't crash."""
    fake_sdk = mock.Mock()
    response = _hf_response(
        tool_name="classify_intent",
        args='{"intent": "DUPLICATE_PO", "confidence": 0.92}',
    )
    response.usage = None
    fake_sdk.chat_completion.return_value = response

    client = HuggingFaceProviderClient(
        sdk_client=fake_sdk, model_id="Qwen/Qwen2.5-32B-Instruct"
    )
    result = client.call_with_tool(
        system=[],
        user_message="u",
        tool_name="classify_intent",
        tool_description="d",
        tool_input_schema={"type": "object"},
    )
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0


# ---------------------------------------------------------------------------
# call_with_tool — error paths
# ---------------------------------------------------------------------------


def test_call_with_tool_no_choices_raises_schema_mismatch() -> None:
    fake_sdk = mock.Mock()
    response = mock.Mock()
    response.choices = []
    fake_sdk.chat_completion.return_value = response

    client = HuggingFaceProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "schema_mismatch"


def test_call_with_tool_missing_tool_call_raises_schema_mismatch() -> None:
    """finish_reason='length' typically — model hit max_tokens."""
    fake_sdk = mock.Mock()
    message = mock.Mock()
    message.tool_calls = []
    choice = mock.Mock()
    choice.finish_reason = "length"
    choice.message = message
    response = mock.Mock()
    response.choices = [choice]
    response.usage = mock.Mock(prompt_tokens=120, completion_tokens=256)
    response.id = None
    response.model = "x"
    fake_sdk.chat_completion.return_value = response

    client = HuggingFaceProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="classify_intent",
            tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "schema_mismatch"


def test_call_with_tool_invalid_json_raises_schema_mismatch() -> None:
    fake_sdk = mock.Mock()
    fake_sdk.chat_completion.return_value = _hf_response(
        tool_name="classify_intent",
        args="{not valid json",
    )
    client = HuggingFaceProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="classify_intent",
            tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "schema_mismatch"


# ---------------------------------------------------------------------------
# Exception classification
# ---------------------------------------------------------------------------


def test_429_classifies_as_rate_limit() -> None:
    class FakeHfHubHTTPError(Exception):
        def __init__(self):
            super().__init__("rate limited")
            self.response = mock.Mock(status_code=429)

    FakeHfHubHTTPError.__name__ = "HfHubHTTPError"

    fake_sdk = mock.Mock()
    fake_sdk.chat_completion.side_effect = FakeHfHubHTTPError()
    client = HuggingFaceProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "rate_limit"
    assert ei.value.retryable is True
    assert ei.value.status_code == 429


def test_503_classifies_as_server_error() -> None:
    class FakeHfHubHTTPError(Exception):
        def __init__(self):
            super().__init__("upstream down")
            self.response = mock.Mock(status_code=503)

    FakeHfHubHTTPError.__name__ = "HfHubHTTPError"

    fake_sdk = mock.Mock()
    fake_sdk.chat_completion.side_effect = FakeHfHubHTTPError()
    client = HuggingFaceProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "server_error"
    assert ei.value.retryable is True


def test_403_classifies_as_auth() -> None:
    class FakeHfHubHTTPError(Exception):
        def __init__(self):
            super().__init__("forbidden")
            self.response = mock.Mock(status_code=403)

    FakeHfHubHTTPError.__name__ = "HfHubHTTPError"

    fake_sdk = mock.Mock()
    fake_sdk.chat_completion.side_effect = FakeHfHubHTTPError()
    client = HuggingFaceProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "auth"
    assert ei.value.retryable is False


def test_inference_timeout_classifies_as_timeout() -> None:
    class FakeInferenceTimeoutError(Exception):
        pass

    FakeInferenceTimeoutError.__name__ = "InferenceTimeoutError"

    fake_sdk = mock.Mock()
    fake_sdk.chat_completion.side_effect = FakeInferenceTimeoutError("timed out")
    client = HuggingFaceProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "timeout"
    assert ei.value.retryable is True


def test_unknown_exception_classifies_as_unknown() -> None:
    fake_sdk = mock.Mock()
    fake_sdk.chat_completion.side_effect = ValueError("totally unexpected")
    client = HuggingFaceProviderClient(sdk_client=fake_sdk, model_id="x")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "unknown"
    assert ei.value.retryable is False
