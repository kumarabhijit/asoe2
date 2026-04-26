from __future__ import annotations

# Coverage for llm/anthropic_client.py
#
# Verifies:
#   - RemoteLLMConfig.from_env reads every supported env var with the
#     provider-prefix pattern (ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL,
#     ANTHROPIC_DEPLOYMENT, ANTHROPIC_EXTRA_HEADERS, etc.)
#   - AnthropicProviderClient.from_config requires api_key
#   - ASOE_ENV=production rejects public Anthropic egress via the
#     class method
#   - Kill switch is checked at build time (no TCP open)
#   - AnthropicProviderClient.call_with_tool maps SystemBlock cache
#     markers, emits the right messages.create kwargs, and parses
#     responses into ToolCallResult
#   - Exceptions classify into ProviderError with the right kind
#   - The legacy build_client() shim still works for back-compat

import builtins
import sys
from unittest import mock

import pytest

from llm.anthropic_client import (
    AnthropicProviderClient,
    ProductionEgressBlocked,
    RemoteLLMConfig,
    build_client,
)
from llm.provider_protocol import CacheControl, ProviderError, SystemBlock


# ---------------------------------------------------------------------------
# RemoteLLMConfig.from_env
# ---------------------------------------------------------------------------


def test_from_env_minimum_required_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-abc")
    for key in (
        "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_DEPLOYMENT",
        "ANTHROPIC_API_VERSION", "ANTHROPIC_REGION", "ANTHROPIC_PROJECT_ID",
        "ANTHROPIC_TIMEOUT_S", "ANTHROPIC_MAX_RETRIES", "ANTHROPIC_EXTRA_HEADERS",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = RemoteLLMConfig.from_env(provider="anthropic")
    assert cfg.api_key == "sk-test-abc"
    assert cfg.base_url is None
    assert cfg.model_id == "claude-sonnet-4-6"  # LLM_DEFAULT_MODEL_ID
    assert cfg.deployment_name is None
    assert cfg.api_version is None
    assert cfg.region is None
    assert cfg.project_id is None
    assert cfg.timeout_s == 30.0  # LLM_CALL_TIMEOUT_S
    assert cfg.max_retries == 2
    assert cfg.extra_headers == ()


def test_from_env_all_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://foundry.example/anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-4-7")
    monkeypatch.setenv("ANTHROPIC_DEPLOYMENT", "asoe-prod-claude")
    monkeypatch.setenv("ANTHROPIC_API_VERSION", "2023-06-01")
    monkeypatch.setenv("ANTHROPIC_REGION", "eastus")
    monkeypatch.setenv("ANTHROPIC_TIMEOUT_S", "45.0")
    monkeypatch.setenv("ANTHROPIC_MAX_RETRIES", "3")
    monkeypatch.setenv(
        "ANTHROPIC_EXTRA_HEADERS",
        "anthropic-beta: task-budgets-2026-03-13; x-tenant-id: t-42",
    )

    cfg = RemoteLLMConfig.from_env(provider="anthropic")
    assert cfg.base_url == "https://foundry.example/anthropic"
    assert cfg.model_id == "claude-opus-4-7"
    assert cfg.deployment_name == "asoe-prod-claude"
    assert cfg.api_version == "2023-06-01"
    assert cfg.region == "eastus"
    assert cfg.timeout_s == 45.0
    assert cfg.max_retries == 3
    assert cfg.extra_headers == (
        ("anthropic-beta", "task-budgets-2026-03-13"),
        ("x-tenant-id", "t-42"),
    )


def test_from_env_provider_prefix_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """OPENAI_* env vars must NOT leak into an anthropic config."""
    # Clear any inherited ANTHROPIC_* state so the assertion is
    # actually about provider isolation, not leftover process env.
    for key in (
        "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_DEPLOYMENT",
        "ANTHROPIC_API_VERSION", "ANTHROPIC_REGION", "ANTHROPIC_PROJECT_ID",
        "ANTHROPIC_TIMEOUT_S", "ANTHROPIC_MAX_RETRIES", "ANTHROPIC_EXTRA_HEADERS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

    cfg = RemoteLLMConfig.from_env(provider="anthropic")
    assert cfg.api_key == "anthropic-key"
    assert cfg.base_url is None  # OPENAI_BASE_URL did not bleed in


def test_from_env_missing_api_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """from_env no longer raises on missing api_key — that's the
    provider's responsibility to check at from_config time. This
    keeps from_env usable for stub providers (e.g. Ollama self-hosted
    has no key)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = RemoteLLMConfig.from_env(provider="anthropic")
    assert cfg.api_key is None


def test_config_is_frozen() -> None:
    cfg = RemoteLLMConfig(api_key="x")
    with pytest.raises(Exception):  # pydantic ValidationError on frozen
        cfg.api_key = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AnthropicProviderClient.from_config — auth + production-egress + kill
# ---------------------------------------------------------------------------


def test_from_config_requires_api_key() -> None:
    cfg = RemoteLLMConfig(api_key=None)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        AnthropicProviderClient.from_config(cfg)


@pytest.mark.parametrize(
    "blocked",
    [None, "https://api.anthropic.com", "https://api.anthropic.com/", "api.anthropic.com"],
)
def test_production_blocks_public_anthropic(blocked: str | None) -> None:
    cfg = RemoteLLMConfig(api_key="x", base_url=blocked)
    with pytest.raises(ProductionEgressBlocked):
        AnthropicProviderClient.from_config(cfg, asoe_env="production")


def test_production_allows_foundry_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    captured: dict[str, object] = {}

    class FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        class messages:  # noqa: D106
            @staticmethod
            def create(**kwargs):
                raise NotImplementedError

    fake = mock.Mock()
    fake.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    cfg = RemoteLLMConfig(
        api_key="x", base_url="https://foundry.private.example/anthropic"
    )
    client = AnthropicProviderClient.from_config(cfg, asoe_env="production")
    assert client.provider_name == "anthropic"


def test_sandbox_allows_public_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sandbox: anything goes — base_url=None is permitted."""
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    fake = mock.Mock()
    fake.Anthropic = mock.Mock(return_value=mock.Mock())
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    cfg = RemoteLLMConfig(api_key="x", base_url=None)
    client = AnthropicProviderClient.from_config(cfg, asoe_env="sandbox")
    assert client.provider_name == "anthropic"


def test_from_config_blocked_by_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "1")
    cfg = RemoteLLMConfig(api_key="x")
    with pytest.raises(RuntimeError, match="ASOE_KILL_SWITCH"):
        AnthropicProviderClient.from_config(cfg, asoe_env="sandbox")


def test_from_config_missing_dep_raises_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "anthropic":
            raise ImportError("No module named 'anthropic'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    cfg = RemoteLLMConfig(api_key="x")
    with pytest.raises(ImportError, match=r"asoe\[anthropic\]"):
        AnthropicProviderClient.from_config(cfg, asoe_env="sandbox")


def test_from_config_passes_extra_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    captured: dict[str, object] = {}

    class FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake = mock.Mock()
    fake.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    cfg = RemoteLLMConfig(
        api_key="sk-test",
        base_url="https://foundry.example/anthropic",
        api_version="2023-06-01",
        deployment_name="asoe-prod-claude",
        extra_headers=(("anthropic-beta", "task-budgets-2026-03-13"),),
        timeout_s=20.0,
        max_retries=1,
    )
    AnthropicProviderClient.from_config(cfg, asoe_env="sandbox")

    assert captured["api_key"] == "sk-test"
    assert captured["timeout"] == 20.0
    assert captured["max_retries"] == 1
    assert captured["base_url"] == "https://foundry.example/anthropic"
    headers = captured["default_headers"]
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["x-azure-deployment"] == "asoe-prod-claude"
    assert headers["anthropic-beta"] == "task-budgets-2026-03-13"


# ---------------------------------------------------------------------------
# AnthropicProviderClient.call_with_tool
# ---------------------------------------------------------------------------


def _build_anthropic_response(*, tool_name: str, args: dict) -> mock.Mock:
    """Construct a fake Anthropic response with a tool_use block."""
    tool_block = mock.Mock()
    tool_block.type = "tool_use"
    tool_block.name = tool_name
    tool_block.input = args

    text_block = mock.Mock()
    text_block.type = "text"
    text_block.text = "ignored"

    resp = mock.Mock()
    resp.content = [text_block, tool_block]
    resp.usage = mock.Mock(
        input_tokens=120,
        output_tokens=15,
        cache_read_input_tokens=6000,
        cache_creation_input_tokens=0,
    )
    resp.stop_reason = "tool_use"
    resp.model = "claude-sonnet-4-6"
    resp._request_id = "req_test_abc123"
    return resp


def test_call_with_tool_maps_cache_control_to_anthropic() -> None:
    """SystemBlock with cache.enabled=True must produce a
    cache_control block in the Anthropic system parameter."""
    fake_sdk = mock.Mock()
    fake_sdk.messages.create.return_value = _build_anthropic_response(
        tool_name="classify_intent",
        args={"intent": "DUPLICATE_PO", "confidence": 0.95},
    )

    client = AnthropicProviderClient(
        sdk_client=fake_sdk, model_id="claude-sonnet-4-6"
    )
    result = client.call_with_tool(
        system=[
            SystemBlock(text="big skill content", cache=CacheControl(enabled=True)),
            SystemBlock(text="more guidance", cache=CacheControl()),
        ],
        user_message="event payload here",
        tool_name="classify_intent",
        tool_description="Classify the order-event intent.",
        tool_input_schema={"type": "object", "properties": {}},
        max_tokens=100,
    )

    assert result.tool_name == "classify_intent"
    assert result.arguments == {"intent": "DUPLICATE_PO", "confidence": 0.95}
    assert result.request_id == "req_test_abc123"
    assert result.usage.input_tokens == 120
    assert result.usage.cache_read_input_tokens == 6000
    assert result.stop_reason == "tool_use"
    assert result.latency_s >= 0
    assert result.model_id == "claude-sonnet-4-6"

    # Inspect the kwargs the SDK would have received.
    fake_sdk.messages.create.assert_called_once()
    kwargs = fake_sdk.messages.create.call_args.kwargs
    sys_params = kwargs["system"]
    assert sys_params[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in sys_params[1]
    # tool_choice forces the named tool.
    assert kwargs["tool_choice"]["type"] == "tool"
    assert kwargs["tool_choice"]["name"] == "classify_intent"
    assert kwargs["tool_choice"]["disable_parallel_tool_use"] is True
    # max_tokens propagates.
    assert kwargs["max_tokens"] == 100


def test_call_with_tool_includes_ttl_when_set() -> None:
    fake_sdk = mock.Mock()
    fake_sdk.messages.create.return_value = _build_anthropic_response(
        tool_name="classify_intent",
        args={"intent": "DUPLICATE_PO", "confidence": 0.9},
    )
    client = AnthropicProviderClient(sdk_client=fake_sdk, model_id="claude-sonnet-4-6")
    client.call_with_tool(
        system=[
            SystemBlock(
                text="x", cache=CacheControl(enabled=True, ttl="1h")
            ),
        ],
        user_message="u",
        tool_name="classify_intent",
        tool_description="d",
        tool_input_schema={"type": "object"},
    )
    sys_params = fake_sdk.messages.create.call_args.kwargs["system"]
    assert sys_params[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_call_with_tool_missing_block_raises_schema_mismatch() -> None:
    """If the response has no tool_use block matching the requested
    name, raise ProviderError(kind='schema_mismatch') so the router
    falls through to the deterministic backend."""
    bad = mock.Mock()
    bad.content = []  # no tool_use
    bad.usage = mock.Mock(input_tokens=10, output_tokens=5,
                          cache_read_input_tokens=0, cache_creation_input_tokens=0)
    bad.stop_reason = "end_turn"
    bad.model = "claude-sonnet-4-6"
    bad._request_id = "req_oops"

    fake_sdk = mock.Mock()
    fake_sdk.messages.create.return_value = bad

    client = AnthropicProviderClient(sdk_client=fake_sdk, model_id="claude-sonnet-4-6")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[],
            user_message="u",
            tool_name="classify_intent",
            tool_description="d",
            tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "schema_mismatch"
    assert ei.value.request_id == "req_oops"


def test_call_with_tool_classifies_sdk_exception() -> None:
    class FakeRateLimitError(Exception):
        pass

    FakeRateLimitError.__name__ = "RateLimitError"

    fake_sdk = mock.Mock()
    fake_sdk.messages.create.side_effect = FakeRateLimitError("429: too many")

    client = AnthropicProviderClient(sdk_client=fake_sdk, model_id="claude-sonnet-4-6")
    with pytest.raises(ProviderError) as ei:
        client.call_with_tool(
            system=[],
            user_message="u",
            tool_name="classify_intent",
            tool_description="d",
            tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "rate_limit"
    assert ei.value.retryable is True


# ---------------------------------------------------------------------------
# Back-compat: build_client() returns the raw SDK client
# ---------------------------------------------------------------------------


def test_build_client_back_compat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")

    class FakeAnthropic:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake = mock.Mock()
    fake.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    cfg = RemoteLLMConfig(api_key="sk-test")
    client = build_client(cfg)
    # Must be the raw SDK client, not the AnthropicProviderClient wrapper
    assert isinstance(client, FakeAnthropic)
    assert client.kwargs["api_key"] == "sk-test"
