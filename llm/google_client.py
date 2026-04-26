from __future__ import annotations

# GoogleProviderClient — Google Vertex AI / Gemini.
#
# **V1 status: stub.** To enable in a follow-up PR:
#   1. Add `google-genai>=1.0.0` (or `google-cloud-aiplatform`) to
#      [project.optional-dependencies] as a `google` extra.
#   2. Lazy-import `from google import genai` in `from_config()`. Use
#      `genai.Client(vertexai=True, project=..., location=...)` for
#      Vertex; `genai.Client(api_key=...)` for direct Gemini API.
#   3. In `call_with_tool()`, use `client.models.generate_content`
#      with `tools=[{"functionDeclarations": [...]}]` and
#      `tool_config={"function_calling_config": {"mode": "ANY",
#      "allowed_function_names": [tool_name]}}`. Map the response
#      `function_calls[0]` into a ToolCallResult.
#   4. Map exceptions to ProviderError.
#   5. Drop this stub block.
#
# Cache control: Vertex has its own cached-content API. SystemBlock
# with cache.enabled=True maps to a separate Cache object created
# once per stable system prefix and referenced by name. Implementation
# detail for the wiring PR.
#
# Production gate: ASOE_ENV=production blocks the public Gemini
# endpoint (`generativelanguage.googleapis.com`); production deploys
# must route via Vertex with a project_id + region.

import os
from typing import Any, Mapping

from llm.provider_protocol import (
    LLMProviderClient,
    ProviderError,
    SystemBlock,
    ToolCallResult,
)


_GOOGLE_PUBLIC_BASE_URLS = frozenset(
    {
        "https://generativelanguage.googleapis.com",
        "https://generativelanguage.googleapis.com/",
        "generativelanguage.googleapis.com",
    }
)


class GoogleProviderClient:
    provider_name: str = "google"

    def __init__(self, sdk_client: Any, model_id: str):
        self._client = sdk_client
        self._model_id = model_id

    @classmethod
    def from_config(cls, config, *, asoe_env: str | None = None):  # noqa: ARG003
        env_resolved = asoe_env if asoe_env is not None else os.getenv("ASOE_ENV", "sandbox")
        if env_resolved == "production":
            # Vertex requires project_id + region. Direct Gemini API
            # (no project_id) is the public path — block in prod.
            if config.project_id is None or config.region is None:
                raise RuntimeError(
                    "ASOE_ENV=production requires Vertex AI configuration "
                    "(GOOGLE_PROJECT_ID + GOOGLE_REGION). Direct Gemini "
                    "API is blocked."
                )
            if config.base_url and config.base_url in _GOOGLE_PUBLIC_BASE_URLS:
                raise RuntimeError(
                    "ASOE_ENV=production rejects egress to "
                    "generativelanguage.googleapis.com."
                )
        raise NotImplementedError(
            "GoogleProviderClient is a V1 stub. Set ASOE_LLM_PROVIDER to "
            "'anthropic' (implemented in V1) or 'fallback' until this "
            "client is wired up. Implementation steps live at the top of "
            "llm/google_client.py."
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
            "GoogleProviderClient.call_with_tool is not implemented in V1.",
            kind="unknown",
            retryable=False,
        )


_check: LLMProviderClient = GoogleProviderClient  # type: ignore[assignment, type-abstract]
del _check


__all__ = ("GoogleProviderClient",)
