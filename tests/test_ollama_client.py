from __future__ import annotations

# Coverage for llm/ollama_client.py — full implementation.
#
# All tests are network-free: the `ollama.Client` class is stubbed
# in `sys.modules['ollama']` so the SDK is never actually imported
# from disk. We exercise:
#   - from_config builds a client with the right host/headers
#   - Production egress gate blocks public Ollama Cloud
#   - Kill switch blocks construction
#   - Missing SDK raises ImportError with install hint
#   - call_with_tool maps system blocks → concatenated system msg
#   - Tool-call args parsed from BOTH dict and JSON-string variants
#   - Token usage mapped from prompt_eval_count / eval_count
#   - Missing tool_call → ProviderError(kind='schema_mismatch')
#   - Exception classification: 429→rate_limit, 5xx→server_error,
#     network→connection, unknown→unknown

import builtins
import sys
from unittest import mock

import pytest

from llm.anthropic_client import RemoteLLMConfig
from llm.ollama_client import OllamaProviderClient
from llm.provider_protocol import CacheControl, ProviderError, SystemBlock


# ---------------------------------------------------------------------------
# from_config — auth + production-egress + kill switch
# ---------------------------------------------------------------------------


def test_from_config_blocks_public_cloud_in_production() -> None:
    cfg = RemoteLLMConfig(api_key="x", base_url="https://api.ollama.com")
    with pytest.raises(RuntimeError, match="Ollama"):
        OllamaProviderClient.from_config(cfg, asoe_env="production")


def test_from_config_blocks_unset_base_url_in_production() -> None:
    cfg = RemoteLLMConfig(api_key="x", base_url=None)
    with pytest.raises(RuntimeError, match="Ollama"):
        OllamaProviderClient.from_config(cfg, asoe_env="production")


def test_from_config_allows_self_hosted_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = mock.Mock()
    fake_module.Client = FakeClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    cfg = RemoteLLMConfig(
        api_key=None,
        base_url="http://my-ollama.private.local:11434",
        model_id="qwen2.5",
    )
    client = OllamaProviderClient.from_config(cfg, asoe_env="production")
    assert client.provider_name == "ollama"
    assert captured["host"] == "http://my-ollama.private.local:11434"
    assert captured["timeout"] == 30.0
    # No api_key → no Authorization header
    assert "headers" not in captured or "Authorization" not in captured.get("headers", {})


def test_from_config_attaches_bearer_when_api_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = mock.Mock()
    fake_module.Client = FakeClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    cfg = RemoteLLMConfig(
        api_key="ollama-cloud-token",
        base_url="https://my-private-cloud.example/",
        model_id="qwen2.5",
        extra_headers=(("x-tenant", "asoe"),),
    )
    OllamaProviderClient.from_config(cfg, asoe_env="sandbox")

    headers = captured["headers"]
    assert headers["Authorization"] == "Bearer ollama-cloud-token"
    assert headers["x-tenant"] == "asoe"


def test_from_config_default_localhost_when_base_url_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In sandbox, no base_url falls through to localhost:11434."""
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = mock.Mock()
    fake_module.Client = FakeClient
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    cfg = RemoteLLMConfig(api_key=None, base_url=None)
    OllamaProviderClient.from_config(cfg, asoe_env="sandbox")
    assert captured["host"] == "http://localhost:11434"


def test_from_config_blocked_by_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "1")
    cfg = RemoteLLMConfig(api_key="x", base_url="http://localhost:11434")
    with pytest.raises(RuntimeError, match="ASOE_KILL_SWITCH"):
        OllamaProviderClient.from_config(cfg, asoe_env="sandbox")


def test_from_config_missing_dep_raises_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")
    monkeypatch.delitem(sys.modules, "ollama", raising=False)

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "ollama":
            raise ImportError("No module named 'ollama'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    cfg = RemoteLLMConfig(api_key="x", base_url="http://localhost:11434")
    with pytest.raises(ImportError, match=r"asoe\[ollama\]"):
        OllamaProviderClient.from_config(cfg, asoe_env="sandbox")


# ---------------------------------------------------------------------------
# call_with_tool — happy paths
# ---------------------------------------------------------------------------


def _ollama_response(*, tool_name: str, args, eval_count: int = 15, prompt_eval_count: int = 120) -> dict:
    """Build a fake Ollama chat() response in the dict shape recent
    versions of the SDK return."""
    return {
        "model": "qwen2.5",
        "created_at": "2026-04-26T00:00:00Z",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": tool_name,
                        "arguments": args,
                    }
                }
            ],
        },
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": prompt_eval_count,
        "eval_count": eval_count,
    }


def test_call_with_tool_parses_dict_arguments() -> None:
    fake_sdk = mock.Mock()
    fake_sdk.chat.return_value = _ollama_response(
        tool_name="classify_intent",
        args={"intent": "DUPLICATE_PO", "confidence": 0.9},
    )

    client = OllamaProviderClient(sdk_client=fake_sdk, model_id="qwen2.5")
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
    assert result.arguments == {"intent": "DUPLICATE_PO", "confidence": 0.9}
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 15
    assert result.usage.cache_read_input_tokens == 0  # ollama doesn't expose
    assert result.usage.cache_creation_input_tokens == 0
    assert result.model_id == "qwen2.5"
    assert result.stop_reason == "stop"
    assert result.request_id is None  # ollama doesn't surface request ids
    assert result.latency_s >= 0

    # Inspect the call args
    fake_sdk.chat.assert_called_once()
    kwargs = fake_sdk.chat.call_args.kwargs
    # System blocks concatenated into a single system message
    msgs = kwargs["messages"]
    assert msgs[0]["role"] == "system"
    assert "big skill content" in msgs[0]["content"]
    assert "more guidance" in msgs[0]["content"]
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "event payload"
    # OpenAI-style tool definition
    assert kwargs["tools"][0]["type"] == "function"
    assert kwargs["tools"][0]["function"]["name"] == "classify_intent"
    assert kwargs["options"]["num_predict"] == 100
    assert kwargs["options"]["temperature"] == 0.0
    assert kwargs["stream"] is False


def test_call_with_tool_parses_json_string_arguments() -> None:
    """Some Ollama versions/models return arguments as a JSON string."""
    fake_sdk = mock.Mock()
    fake_sdk.chat.return_value = _ollama_response(
        tool_name="classify_intent",
        args='{"intent": "DUPLICATE_PO", "confidence": 0.9}',
    )

    client = OllamaProviderClient(sdk_client=fake_sdk, model_id="qwen2.5")
    result = client.call_with_tool(
        system=[SystemBlock(text="x")],
        user_message="u",
        tool_name="classify_intent",
        tool_description="d",
        tool_input_schema={"type": "object"},
    )
    assert result.arguments == {"intent": "DUPLICATE_PO", "confidence": 0.9}


def test_call_with_tool_handles_none_arguments() -> None:
    fake_sdk = mock.Mock()
    fake_sdk.chat.return_value = _ollama_response(
        tool_name="classify_intent",
        args=None,
    )
    client = OllamaProviderClient(sdk_client=fake_sdk, model_id="qwen2.5")
    result = client.call_with_tool(
        system=[],
        user_message="u",
        tool_name="classify_intent",
        tool_description="d",
        tool_input_schema={"type": "object"},
    )
    assert result.arguments == {}


def test_call_with_tool_works_with_attr_access_response() -> None:
    """Some SDK versions return typed objects rather than plain dicts.
    The client must handle both via _get()."""
    class AttrFunction:
        name = "classify_intent"
        arguments = {"intent": "DUPLICATE_PO", "confidence": 0.9}

    class AttrToolCall:
        function = AttrFunction()

    class AttrMessage:
        role = "assistant"
        content = ""
        tool_calls = [AttrToolCall()]

    class AttrResponse:
        model = "qwen2.5"
        message = AttrMessage()
        done = True
        done_reason = "stop"
        prompt_eval_count = 120
        eval_count = 15

    fake_sdk = mock.Mock()
    fake_sdk.chat.return_value = AttrResponse()

    client = OllamaProviderClient(sdk_client=fake_sdk, model_id="qwen2.5")
    result = client.call_with_tool(
        system=[],
        user_message="u",
        tool_name="classify_intent",
        tool_description="d",
        tool_input_schema={"type": "object"},
    )
    assert result.arguments == {"intent": "DUPLICATE_PO", "confidence": 0.9}
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 15


# ---------------------------------------------------------------------------
# call_with_tool — error paths
# ---------------------------------------------------------------------------


def test_call_with_tool_missing_tool_call_raises_schema_mismatch() -> None:
    """Model went `done_reason='length'` and hit max_tokens before
    completing the tool call → no tool_calls in the response."""
    fake_sdk = mock.Mock()
    fake_sdk.chat.return_value = {
        "model": "qwen2.5",
        "message": {"role": "assistant", "content": "I think...", "tool_calls": []},
        "done": True,
        "done_reason": "length",
        "prompt_eval_count": 120,
        "eval_count": 256,
    }
    client = OllamaProviderClient(sdk_client=fake_sdk, model_id="qwen2.5")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[],
            user_message="u",
            tool_name="classify_intent",
            tool_description="d",
            tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "schema_mismatch"
    assert ei.value.retryable is False


def test_call_with_tool_invalid_json_arguments_raises_schema_mismatch() -> None:
    fake_sdk = mock.Mock()
    fake_sdk.chat.return_value = _ollama_response(
        tool_name="classify_intent",
        args="{not valid json",
    )
    client = OllamaProviderClient(sdk_client=fake_sdk, model_id="qwen2.5")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[],
            user_message="u",
            tool_name="classify_intent",
            tool_description="d",
            tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "schema_mismatch"


def test_call_with_tool_unknown_args_type_raises_schema_mismatch() -> None:
    fake_sdk = mock.Mock()
    fake_sdk.chat.return_value = _ollama_response(
        tool_name="classify_intent",
        args=123.456,  # neither dict nor str
    )
    client = OllamaProviderClient(sdk_client=fake_sdk, model_id="qwen2.5")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[],
            user_message="u",
            tool_name="classify_intent",
            tool_description="d",
            tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "schema_mismatch"


# ---------------------------------------------------------------------------
# Exception classification
# ---------------------------------------------------------------------------


def test_429_classifies_as_rate_limit() -> None:
    class FakeResponseError(Exception):
        status_code = 429

    FakeResponseError.__name__ = "ResponseError"

    fake_sdk = mock.Mock()
    fake_sdk.chat.side_effect = FakeResponseError("rate limited")
    client = OllamaProviderClient(sdk_client=fake_sdk, model_id="qwen2.5")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "rate_limit"
    assert ei.value.retryable is True
    assert ei.value.status_code == 429


def test_500_classifies_as_server_error() -> None:
    class FakeResponseError(Exception):
        status_code = 503

    FakeResponseError.__name__ = "ResponseError"

    fake_sdk = mock.Mock()
    fake_sdk.chat.side_effect = FakeResponseError("upstream down")
    client = OllamaProviderClient(sdk_client=fake_sdk, model_id="qwen2.5")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "server_error"
    assert ei.value.retryable is True


def test_401_classifies_as_auth() -> None:
    class FakeResponseError(Exception):
        status_code = 401

    FakeResponseError.__name__ = "ResponseError"

    fake_sdk = mock.Mock()
    fake_sdk.chat.side_effect = FakeResponseError("bad token")
    client = OllamaProviderClient(sdk_client=fake_sdk, model_id="qwen2.5")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "auth"
    assert ei.value.retryable is False


def test_connection_error_classifies_as_connection() -> None:
    class FakeConnError(Exception):
        pass

    FakeConnError.__name__ = "ConnectError"

    fake_sdk = mock.Mock()
    fake_sdk.chat.side_effect = FakeConnError("refused")
    client = OllamaProviderClient(sdk_client=fake_sdk, model_id="qwen2.5")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "connection"
    assert ei.value.retryable is True


def test_unknown_exception_classifies_as_unknown() -> None:
    fake_sdk = mock.Mock()
    fake_sdk.chat.side_effect = RuntimeError("totally unexpected")
    client = OllamaProviderClient(sdk_client=fake_sdk, model_id="qwen2.5")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[], user_message="u",
            tool_name="t", tool_description="d", tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "unknown"
    assert ei.value.retryable is False
