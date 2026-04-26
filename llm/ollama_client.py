from __future__ import annotations

# OllamaProviderClient — Ollama cloud or self-hosted.
#
# Ollama exposes an OpenAI-compatible chat-completions endpoint (with
# tool calling on supported models). The OpenAI Python SDK itself
# works against Ollama by setting `base_url=http://localhost:11434/v1`
# (or the cloud URL) and `api_key="ollama"` (any non-empty string is
# accepted). Implementation in a follow-up PR can re-use the OpenAI
# client path with a different base_url.
#
# **V1 status: stub.** Implementation steps:
#   1. Reuse OpenAIProviderClient by pointing it at Ollama's URL,
#      OR add a thin wrapper here that calls the Ollama Python SDK
#      directly (`ollama.chat(...)` with `tools=[...]`).
#   2. Cache control: not supported by Ollama; SystemBlock.cache
#      markers are silently ignored.
#   3. Token usage: Ollama returns prompt_eval_count + eval_count.
#      Map to TokenUsage(input_tokens, output_tokens).
#   4. No api_key required for self-hosted; the cloud-managed Ollama
#      requires a bearer token.
#
# Production gate: V1 policy is permissive for self-hosted Ollama
# (it's behind your own VPC). Cloud Ollama (ollama.com) gets the same
# treatment as any third-party SaaS — block in production unless the
# operator has signed a DPA and configured a private peering route.

import os
from typing import Any, Mapping

from llm.provider_protocol import (
    LLMProviderClient,
    ProviderError,
    SystemBlock,
    ToolCallResult,
)


_OLLAMA_PUBLIC_CLOUD_URLS = frozenset(
    {
        "https://ollama.com",
        "https://api.ollama.com",
        "ollama.com",
        "api.ollama.com",
    }
)


class OllamaProviderClient:
    provider_name: str = "ollama"

    def __init__(self, sdk_client: Any, model_id: str):
        self._client = sdk_client
        self._model_id = model_id

    @classmethod
    def from_config(cls, config, *, asoe_env: str | None = None):  # noqa: ARG003
        env_resolved = asoe_env if asoe_env is not None else os.getenv("ASOE_ENV", "sandbox")
        if env_resolved == "production" and (
            config.base_url is None or config.base_url in _OLLAMA_PUBLIC_CLOUD_URLS
        ):
            raise RuntimeError(
                "ASOE_ENV=production rejects egress to public Ollama "
                "Cloud. Set OLLAMA_BASE_URL to a self-hosted or "
                "private-peered Ollama endpoint."
            )
        raise NotImplementedError(
            "OllamaProviderClient is a V1 stub. Set ASOE_LLM_PROVIDER to "
            "'anthropic' (implemented in V1) or 'fallback' until this "
            "client is wired up. Implementation steps live at the top of "
            "llm/ollama_client.py."
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
            "OllamaProviderClient.call_with_tool is not implemented in V1.",
            kind="unknown",
            retryable=False,
        )


_check: LLMProviderClient = OllamaProviderClient  # type: ignore[assignment, type-abstract]
del _check


__all__ = ("OllamaProviderClient",)
