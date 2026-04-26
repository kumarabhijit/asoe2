from __future__ import annotations

# OpenAIProviderClient — works with OpenAI direct API and Azure OpenAI.
#
# **V1 status: stub.** The Protocol implementation is wired but
# `call_with_tool()` raises NotImplementedError. To enable in a follow-
# up PR:
#   1. Add `openai>=1.50.0` to pyproject.toml [project.optional-deps]
#      under an `openai` extra.
#   2. Lazy-import `openai.OpenAI` (direct) or `openai.AzureOpenAI`
#      (Azure) in `from_config()`. Pick by base_url presence + the
#      api_version field.
#   3. In `call_with_tool()`, build a `chat.completions.create` request
#      with `tools` + `tool_choice={"type":"function", "function":
#      {"name": tool_name}}`. Map the response message.tool_calls[0]
#      into a ToolCallResult.
#   4. Map exceptions to ProviderError per `_OPENAI_KIND_BY_EXC`.
#   5. Drop this comment block, mark the test file as no-longer-skip.
#
# Cache control: OpenAI prompt caching is automatic on supported
# models (gpt-4o, gpt-4o-mini); SystemBlock.cache markers are honored
# implicitly. Concatenate cacheable blocks first; OpenAI caches the
# stable prefix and bills cached tokens at 50% (vs Anthropic's 90%).
#
# Production gate: when ASOE_ENV=production, public api.openai.com is
# blocked the same way as api.anthropic.com — production must use a
# private Azure OpenAI endpoint.

import os
from typing import Any, Mapping

from llm.provider_protocol import (
    LLMProviderClient,
    ProviderError,
    SystemBlock,
    ToolCallResult,
)


_OPENAI_PUBLIC_BASE_URLS = frozenset(
    {
        "https://api.openai.com",
        "https://api.openai.com/",
        "api.openai.com",
        "https://api.openai.com/v1",
    }
)


class _StubNotImplemented(NotImplementedError):
    """Raised when a stubbed provider's call_with_tool is invoked.
    The router catches NotImplementedError as a ProviderError with
    kind='unknown' and falls through to deterministic — but operators
    really shouldn't set ASOE_LLM_PROVIDER=openai until this lands."""


class OpenAIProviderClient:
    provider_name: str = "openai"

    def __init__(self, sdk_client: Any, model_id: str):
        self._client = sdk_client
        self._model_id = model_id

    @classmethod
    def from_config(cls, config, *, asoe_env: str | None = None):  # noqa: ARG003
        env_resolved = asoe_env if asoe_env is not None else os.getenv("ASOE_ENV", "sandbox")
        if env_resolved == "production" and (
            config.base_url is None or config.base_url in _OPENAI_PUBLIC_BASE_URLS
        ):
            raise RuntimeError(
                "ASOE_ENV=production rejects egress to api.openai.com. "
                "Set OPENAI_BASE_URL to an allowlisted Azure OpenAI "
                "private endpoint."
            )
        raise _StubNotImplemented(
            "OpenAIProviderClient is a V1 stub. Set ASOE_LLM_PROVIDER to "
            "'anthropic' (the implemented V1 provider) or 'fallback' "
            "until this client is wired up. Implementation steps live "
            "at the top of llm/openai_client.py."
        )

    def call_with_tool(
        self,
        *,
        system: list[SystemBlock],
        user_message: str,
        tool_name: str,
        tool_description: str,
        tool_input_schema: Mapping[str, Any],
        max_tokens: int = 256,
    ) -> ToolCallResult:
        raise ProviderError(
            "OpenAIProviderClient.call_with_tool is not implemented in V1.",
            kind="unknown",
            retryable=False,
        )


# Type-time Protocol satisfaction check.
_check: LLMProviderClient = OpenAIProviderClient  # type: ignore[assignment, type-abstract]
del _check


__all__ = ("OpenAIProviderClient",)
