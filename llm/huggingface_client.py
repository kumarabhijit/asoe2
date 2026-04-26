from __future__ import annotations

# HuggingFaceProviderClient — HuggingFace Inference Endpoints / HF
# Inference API.
#
# Backed by `huggingface_hub.InferenceClient` (lazy-imported). The
# SDK exposes an OpenAI-compatible chat-completions surface with
# tool calling on supported models (Qwen2.5-Instruct, Llama 3.1+,
# Mistral Large 2, Mixtral 8x22B).
#
# Two deployment shapes:
#   - **Dedicated Inference Endpoint** (private, paid, production-
#     ready): set HUGGINGFACE_BASE_URL=
#     https://<endpoint>.endpoints.huggingface.cloud
#     plus HUGGINGFACE_API_KEY (an hf_xxx token). The InferenceClient
#     routes to the endpoint URL.
#   - **Serverless Inference API** (shared, free-tier rate-limited):
#     leave HUGGINGFACE_BASE_URL unset; HF's SDK routes to the
#     shared endpoint. ASOE_ENV=production blocks this path.
#
# **Self-hosted vLLM / TGI clusters running an HF model** are NOT
# this provider — those use ASOE_LLM_PROVIDER=openai with
# OPENAI_BASE_URL pointing at the cluster (vLLM/TGI expose an
# OpenAI-compatible chat-completions endpoint and don't need an HF
# auth token).
#
# Cache control: HF Inference does not expose prompt caching to
# clients. SystemBlock.cache markers are silently ignored — system
# blocks are concatenated into a single system message.
#
# Token usage: `response.usage.prompt_tokens` →
# input_tokens; `response.usage.completion_tokens` → output_tokens.
# Cache fields stay zero. Pricing entries for HF-served models
# (Qwen, Llama, Mistral) can be added to
# contracts/policy.py LLM_PRICING_USD_PER_M_TOKENS in a follow-up;
# until then `estimate_cost_usd` returns 0.0 for HF-served models
# (operator must configure pricing for accurate budget tracking).
#
# `tool_calls[0].function.arguments` is a JSON STRING in the HF
# response (OpenAI-compatible shape); we parse with json.loads and
# fall through to schema_mismatch on a parse error.

import json
import logging
import os
import time
from typing import Any, Mapping

from llm.provider_protocol import (
    LLMProviderClient,
    ProviderError,
    SystemBlock,
    TokenUsage,
    ToolCallResult,
)

logger = logging.getLogger("asoe.llm.huggingface")


_HF_PUBLIC_BASE_URLS = frozenset(
    {
        "https://api-inference.huggingface.co",
        "https://api-inference.huggingface.co/",
        "api-inference.huggingface.co",
    }
)
"""HF Serverless Inference API URLs blocked in production. Dedicated
Inference Endpoints have customer-specific URLs of the form
`https://<endpoint>.endpoints.huggingface.cloud` and are permitted
(the operator owns that endpoint)."""


_HF_KIND_BY_EXC: dict[str, str] = {
    "InferenceTimeoutError": "timeout",
    "BadRequestError": "schema_mismatch",
    "HfHubHTTPError": "server_error",
    "HTTPError": "server_error",
    "ConnectError": "connection",
    "ConnectionError": "connection",
    "TimeoutException": "timeout",
    "ReadTimeout": "timeout",
}


def _classify_hf_exc(exc: BaseException) -> tuple[str, bool, int | None]:
    """Classify an HF SDK / httpx exception into (kind, retryable,
    status_code)."""
    name = type(exc).__name__
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
    if status_code == 429:
        return "rate_limit", True, 429
    if status_code in (401, 403):
        return "auth", False, status_code
    if status_code == 400:
        return "schema_mismatch", False, 400
    if status_code is not None and 500 <= status_code < 600:
        return "server_error", True, status_code
    kind = _HF_KIND_BY_EXC.get(name, "unknown")
    retryable = kind in {"timeout", "connection", "server_error", "rate_limit"}
    return kind, retryable, status_code


class HuggingFaceProviderClient:
    """LLMProviderClient implementation backed by
    `huggingface_hub.InferenceClient`."""

    provider_name: str = "huggingface"

    def __init__(self, sdk_client: Any, model_id: str):
        self._client = sdk_client
        self._model_id = model_id

    @classmethod
    def from_config(
        cls, config, *, asoe_env: str | None = None
    ) -> "HuggingFaceProviderClient":
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

        from hardening.kill_switch import is_kill_switch_active  # noqa: PLC0415

        if is_kill_switch_active():
            raise RuntimeError(
                "ASOE_KILL_SWITCH is active; HuggingFace client construction "
                "blocked. No outbound TCP opened."
            )

        if not config.api_key:
            raise ValueError(
                "HUGGINGFACE_API_KEY is required for the huggingface "
                "provider. Generate a token at https://huggingface.co/"
                "settings/tokens with 'Inference' permission."
            )

        try:
            from huggingface_hub import InferenceClient  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "huggingface_hub SDK is required when "
                "ASOE_LLM_PROVIDER=huggingface. Install with: "
                "pip install 'asoe[huggingface]'"
            ) from exc

        # The InferenceClient routes by `base_url` when set (Dedicated
        # Inference Endpoint) OR by `model` (Serverless Inference API
        # → resolves the model id to the shared endpoint).
        kwargs: dict[str, Any] = {
            "token": config.api_key,
            "timeout": config.timeout_s,
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        else:
            kwargs["model"] = config.model_id
        # Forward extra headers to the underlying httpx session.
        if config.extra_headers:
            kwargs["headers"] = {k: v for k, v in config.extra_headers}

        sdk_client = InferenceClient(**kwargs)
        return cls(sdk_client=sdk_client, model_id=config.model_id)

    # ------------------------------------------------------------------
    # Protocol surface
    # ------------------------------------------------------------------

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
        # HF uses OpenAI-style messages. Cache markers ignored —
        # concatenate.
        system_text = "\n\n".join(b.text for b in system)
        messages = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_message},
        ]
        tool_def = {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": tool_description,
                "parameters": dict(tool_input_schema),
            },
        }

        started = time.monotonic()
        try:
            raw = self._client.chat_completion(
                messages=messages,
                tools=[tool_def],
                tool_choice={"type": "function", "function": {"name": tool_name}},
                max_tokens=max_tokens,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001
            kind, retryable, status_code = _classify_hf_exc(exc)
            raise ProviderError(
                f"HuggingFace call_with_tool failed: {type(exc).__name__}: {exc}",
                kind=kind,
                retryable=retryable,
                status_code=status_code,
                request_id=getattr(exc, "request_id", None),
            ) from exc

        latency_s = time.monotonic() - started

        # OpenAI-compatible response shape:
        #   raw.choices[0].message.tool_calls[0].function.{name,arguments}
        #   raw.usage.{prompt_tokens, completion_tokens}
        choices = _get(raw, "choices") or []
        if not choices:
            raise ProviderError(
                "HuggingFace response had no choices.",
                kind="schema_mismatch",
                retryable=False,
            )
        choice = choices[0]
        message = _get(choice, "message")
        if message is None:
            raise ProviderError(
                "HuggingFace response choice had no message.",
                kind="schema_mismatch",
                retryable=False,
            )
        tool_calls = _get(message, "tool_calls") or []
        tool_block = None
        for tc in tool_calls:
            fn = _get(tc, "function")
            if fn is not None and _get(fn, "name") == tool_name:
                tool_block = fn
                break
        if tool_block is None:
            raise ProviderError(
                f"HuggingFace response did not contain a tool_call for "
                f"{tool_name!r}. finish_reason={_get(choice, 'finish_reason')!r}",
                kind="schema_mismatch",
                retryable=False,
            )

        # `arguments` is typically a JSON string from HF (OpenAI-compatible).
        args_raw = _get(tool_block, "arguments")
        if isinstance(args_raw, str):
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    f"HuggingFace returned non-JSON tool arguments: {args_raw!r}",
                    kind="schema_mismatch",
                    retryable=False,
                ) from exc
        elif isinstance(args_raw, Mapping):
            args = dict(args_raw)
        elif args_raw is None:
            args = {}
        else:
            raise ProviderError(
                f"HuggingFace returned tool arguments of unexpected type "
                f"{type(args_raw).__name__}",
                kind="schema_mismatch",
                retryable=False,
            )

        usage_obj = _get(raw, "usage")
        usage = TokenUsage(
            input_tokens=int(_get(usage_obj, "prompt_tokens") or 0)
            if usage_obj is not None
            else 0,
            output_tokens=int(_get(usage_obj, "completion_tokens") or 0)
            if usage_obj is not None
            else 0,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )

        return ToolCallResult(
            tool_name=tool_name,
            arguments=args,
            request_id=str(_get(raw, "id")) if _get(raw, "id") is not None else None,
            model_id=str(_get(raw, "model") or self._model_id),
            usage=usage,
            latency_s=latency_s,
            stop_reason=str(_get(choice, "finish_reason") or ""),
        )


def _get(obj: Any, key: str) -> Any:
    """Read `key` from an SDK response that may be a dict or a typed
    attr-accessible object."""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


# Type-time Protocol satisfaction check.
_check: LLMProviderClient = HuggingFaceProviderClient  # type: ignore[assignment, type-abstract]
del _check


__all__ = ("HuggingFaceProviderClient",)
