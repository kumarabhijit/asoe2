from __future__ import annotations

# HuggingFaceProviderClient — HuggingFace Inference Endpoints / HF
# Inference API.
#
# Targets HF's hosted inference. For Qwen / Llama / Mistral / any HF
# model that the operator wants reachable via `huggingface_hub`'s
# InferenceClient. Two deployment shapes:
#   - **Dedicated Inference Endpoint** (private, paid): set
#     HUGGINGFACE_BASE_URL=https://<endpoint>.endpoints.huggingface.cloud
#     plus HUGGINGFACE_API_KEY (a hf_xxx token).
#   - **Serverless Inference API** (shared, rate-limited): leave
#     HUGGINGFACE_BASE_URL unset; HF's SDK routes to the free tier.
#     Production-egress blocked unless explicitly opted-in.
#
# **NOT for self-hosted vLLM / TGI clusters** running an HF model
# — those expose an OpenAI-compatible chat-completions endpoint and
# should use the `openai` provider with OPENAI_BASE_URL pointing at
# the cluster. That path doesn't need an HF auth token and re-uses
# the OpenAI Python SDK.
#
# **V1 status: stub.** Implementation steps:
#   1. Add `huggingface_hub>=0.27.0` to pyproject.toml
#      [project.optional-dependencies] under a `huggingface` extra.
#   2. Lazy-import `from huggingface_hub import InferenceClient` in
#      from_config(). Construct with
#      `InferenceClient(model=config.model_id, token=config.api_key,
#      base_url=config.base_url, timeout=config.timeout_s)`.
#   3. In call_with_tool(), call `client.chat_completion(messages,
#      tools, tool_choice, max_tokens, temperature=0.0)`. The HF
#      InferenceClient supports OpenAI-style tool calling on models
#      that advertise it (Qwen2.5-Instruct, Llama 3.1+, Mistral
#      Large 2). Map the response choice.message.tool_calls[0] into
#      a ToolCallResult.
#   4. Cache control: HF Inference does not expose prompt caching
#      to clients; SystemBlock.cache markers are silently ignored.
#      Concatenate cacheable + non-cacheable system blocks.
#   5. Token usage: response.usage maps to TokenUsage
#      (input_tokens, output_tokens; cache fields stay zero).
#   6. Map exceptions: HfHubHTTPError → ProviderError with kind
#      derived from status_code (429→rate_limit, 5xx→server_error,
#      etc.). InferenceTimeoutError → kind='timeout', retryable.
#   7. Drop this stub block and remove the test-skip marker.
#
# Production gate: ASOE_ENV=production blocks the public Serverless
# Inference API base URL — production deploys must use a Dedicated
# Inference Endpoint URL.

import os
from typing import Any, Mapping

from llm.provider_protocol import (
    LLMProviderClient,
    ProviderError,
    SystemBlock,
    ToolCallResult,
)


_HF_PUBLIC_BASE_URLS = frozenset(
    {
        "https://api-inference.huggingface.co",
        "https://api-inference.huggingface.co/",
        "api-inference.huggingface.co",
    }
)
"""HF Serverless Inference API base URLs blocked in production.
Dedicated Inference Endpoints have customer-specific URLs of the
form `https://<endpoint>.endpoints.huggingface.cloud` and are
permitted (the operator owns that endpoint)."""


class HuggingFaceProviderClient:
    provider_name: str = "huggingface"

    def __init__(self, sdk_client: Any, model_id: str):
        self._client = sdk_client
        self._model_id = model_id

    @classmethod
    def from_config(cls, config, *, asoe_env: str | None = None):  # noqa: ARG003
        env_resolved = asoe_env if asoe_env is not None else os.getenv("ASOE_ENV", "sandbox")
        if env_resolved == "production":
            # Production must use a Dedicated Inference Endpoint URL.
            # base_url=None means the SDK uses the public Serverless
            # Inference API — block.
            if config.base_url is None or config.base_url in _HF_PUBLIC_BASE_URLS:
                raise RuntimeError(
                    "ASOE_ENV=production rejects egress to the public "
                    "HuggingFace Serverless Inference API. Set "
                    "HUGGINGFACE_BASE_URL to a Dedicated Inference "
                    "Endpoint URL (https://<endpoint>.endpoints."
                    "huggingface.cloud)."
                )
        raise NotImplementedError(
            "HuggingFaceProviderClient is a V1 stub. Set "
            "ASOE_LLM_PROVIDER to 'anthropic' (the implemented V1 "
            "provider), 'openai' (works for self-hosted vLLM/TGI "
            "running Qwen/Llama/Mistral), or 'fallback' until this "
            "client is wired up. Implementation steps live at the "
            "top of llm/huggingface_client.py."
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
            "HuggingFaceProviderClient.call_with_tool is not implemented in V1.",
            kind="unknown",
            retryable=False,
        )


_check: LLMProviderClient = HuggingFaceProviderClient  # type: ignore[assignment, type-abstract]
del _check


__all__ = ("HuggingFaceProviderClient",)
