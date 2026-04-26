from __future__ import annotations

# OpenAIProviderClient — works with three deployment shapes:
#
#   1. **OpenAI direct** (api.openai.com): sandbox/dev only (production-
#      blocked unless explicitly opted-in via base_url).
#         OPENAI_API_KEY=sk-...
#         (no OPENAI_BASE_URL, no OPENAI_API_VERSION)
#
#   2. **Azure OpenAI** (private endpoint, production-eligible):
#         OPENAI_API_KEY=<azure key>
#         OPENAI_BASE_URL=https://<resource>.openai.azure.com
#         OPENAI_API_VERSION=2024-02-01
#         OPENAI_DEPLOYMENT=<azure deployment name>
#      (api_version presence selects the AzureOpenAI client class.)
#
#   3. **OpenAI-compatible self-hosted** (vLLM, TGI, LiteLLM proxy,
#      LocalAI, anyscale, fireworks, together, groq, etc.) running
#      any model — including Qwen / Llama / Mistral served by vLLM:
#         OPENAI_API_KEY=<placeholder, often any non-empty string>
#         OPENAI_BASE_URL=https://my-vllm-cluster.example/v1
#         OPENAI_MODEL=Qwen/Qwen2.5-32B-Instruct
#      No api_version → OpenAI() client; the base_url override routes
#      to the local cluster.
#
# Tool calling: standard OpenAI shape — `tools` + `tool_choice=
# {"type": "function", "function": {"name": "..."}}`. Response
# `tool_calls[0].function.arguments` is always a JSON STRING.
#
# Cache control: OpenAI prompt caching is AUTOMATIC on supported
# models (gpt-4o, gpt-4o-mini, o1) for prompts >1024 tokens. We
# don't pass cache_control markers (the API rejects them); we DO
# read `usage.prompt_tokens_details.cached_tokens` when the field is
# present and populate `cache_read_input_tokens`.
#
# Cost tracking: contracts/policy.py LLM_PRICING_USD_PER_M_TOKENS
# only carries Claude entries today. OpenAI / Azure OpenAI / vLLM
# entries can be added in a follow-up; until then estimate_cost_usd
# returns 0.0 for OpenAI-served models (operator must populate
# pricing for accurate budget tracking).

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

logger = logging.getLogger("asoe.llm.openai")


_OPENAI_PUBLIC_BASE_URLS = frozenset(
    {
        "https://api.openai.com",
        "https://api.openai.com/",
        "https://api.openai.com/v1",
        "https://api.openai.com/v1/",
        "api.openai.com",
    }
)
"""Public OpenAI URLs blocked in production. Prod must route via
Azure OpenAI private endpoint or another allowlisted base_url."""


_OPENAI_KIND_BY_EXC: dict[str, str] = {
    "RateLimitError": "rate_limit",
    "APITimeoutError": "timeout",
    "APIConnectionError": "connection",
    "AuthenticationError": "auth",
    "PermissionDeniedError": "auth",
    "NotFoundError": "unknown",
    "BadRequestError": "schema_mismatch",
    "UnprocessableEntityError": "schema_mismatch",
    "ConflictError": "schema_mismatch",
    "InternalServerError": "server_error",
    "APIStatusError": "server_error",
}


def _classify_openai_exc(exc: BaseException) -> tuple[str, bool, int | None]:
    name = type(exc).__name__
    status_code = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if status_code == 429:
        return "rate_limit", True, 429
    if status_code in (401, 403):
        return "auth", False, status_code
    if status_code == 400:
        return "schema_mismatch", False, 400
    if status_code is not None and 500 <= status_code < 600:
        return "server_error", True, status_code
    kind = _OPENAI_KIND_BY_EXC.get(name, "unknown")
    retryable = kind in {"timeout", "connection", "server_error", "rate_limit"}
    return kind, retryable, status_code


class OpenAIProviderClient:
    """LLMProviderClient implementation backed by `openai.OpenAI` or
    `openai.AzureOpenAI`. Selection happens once at from_config()
    time; the instance is provider-flavour agnostic afterward."""

    provider_name: str = "openai"

    def __init__(self, sdk_client: Any, model_id: str, *, is_azure: bool = False):
        self._client = sdk_client
        self._model_id = model_id
        self._is_azure = is_azure

    @classmethod
    def from_config(
        cls, config, *, asoe_env: str | None = None
    ) -> "OpenAIProviderClient":
        env_resolved = asoe_env if asoe_env is not None else os.getenv("ASOE_ENV", "sandbox")
        if env_resolved == "production" and (
            config.base_url is None or config.base_url in _OPENAI_PUBLIC_BASE_URLS
        ):
            raise RuntimeError(
                "ASOE_ENV=production rejects egress to api.openai.com. "
                "Set OPENAI_BASE_URL to an allowlisted Azure OpenAI "
                "private endpoint or another approved base URL."
            )

        from hardening.kill_switch import is_kill_switch_active  # noqa: PLC0415

        if is_kill_switch_active():
            raise RuntimeError(
                "ASOE_KILL_SWITCH is active; OpenAI client construction "
                "blocked. No outbound TCP opened."
            )

        if not config.api_key:
            raise ValueError(
                "OPENAI_API_KEY is required for the openai provider. "
                "Self-hosted OpenAI-compatible APIs (vLLM, TGI, LiteLLM) "
                "typically accept any non-empty placeholder."
            )

        try:
            import openai as openai_sdk  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "openai SDK is required when ASOE_LLM_PROVIDER=openai. "
                "Install with: pip install 'asoe[openai]'"
            ) from exc

        # api_version presence selects the Azure OpenAI client class —
        # only Azure OpenAI requires an explicit api_version.
        is_azure = config.api_version is not None

        # Common kwargs go through both client classes.
        common: dict[str, Any] = {
            "api_key": config.api_key,
            "timeout": config.timeout_s,
            "max_retries": config.max_retries,
        }
        if config.extra_headers:
            common["default_headers"] = {k: v for k, v in config.extra_headers}

        if is_azure:
            if not config.base_url:
                raise ValueError(
                    "Azure OpenAI requires OPENAI_BASE_URL to point at the "
                    "Azure resource endpoint "
                    "(https://<resource>.openai.azure.com)."
                )
            azure_kwargs: dict[str, Any] = {
                **common,
                "api_version": config.api_version,
                "azure_endpoint": config.base_url,
            }
            if config.deployment_name:
                # Default deployment makes `model=` optional on calls;
                # we still pass model explicitly so the per-task
                # routing is visible in trace records.
                azure_kwargs["azure_deployment"] = config.deployment_name
            sdk_client = openai_sdk.AzureOpenAI(**azure_kwargs)
        else:
            openai_kwargs: dict[str, Any] = {**common}
            if config.base_url:
                openai_kwargs["base_url"] = config.base_url
            sdk_client = openai_sdk.OpenAI(**openai_kwargs)

        # When using Azure, the chat.completions.create `model` arg
        # is the deployment name (not the underlying model id). Fall
        # back to model_id when deployment_name isn't set.
        effective_model = (
            config.deployment_name if is_azure and config.deployment_name else config.model_id
        )
        return cls(
            sdk_client=sdk_client,
            model_id=effective_model,
            is_azure=is_azure,
        )

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
        # OpenAI uses chat-style messages. Cache markers are not
        # passed (OpenAI handles caching automatically for prompts
        # >1024 tokens on supported models). Concatenate.
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
            raw = self._client.chat.completions.create(
                model=self._model_id,
                messages=messages,
                tools=[tool_def],
                tool_choice={"type": "function", "function": {"name": tool_name}},
                max_tokens=max_tokens,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001
            kind, retryable, status_code = _classify_openai_exc(exc)
            request_id = (
                getattr(exc, "request_id", None)
                or getattr(getattr(exc, "response", None), "headers", {}).get("x-request-id")
                if hasattr(exc, "response")
                else None
            )
            raise ProviderError(
                f"OpenAI call_with_tool failed: {type(exc).__name__}: {exc}",
                kind=kind,
                retryable=retryable,
                status_code=status_code,
                request_id=request_id,
            ) from exc

        latency_s = time.monotonic() - started

        # OpenAI-compatible response shape:
        #   raw.choices[0].message.tool_calls[0].function.{name,arguments}
        #   raw.usage.{prompt_tokens, completion_tokens,
        #              prompt_tokens_details.cached_tokens}
        choices = _get(raw, "choices") or []
        if not choices:
            raise ProviderError(
                "OpenAI response had no choices.",
                kind="schema_mismatch",
                retryable=False,
                request_id=_get_request_id(raw),
            )
        choice = choices[0]
        message = _get(choice, "message")
        if message is None:
            raise ProviderError(
                "OpenAI response choice had no message.",
                kind="schema_mismatch",
                retryable=False,
                request_id=_get_request_id(raw),
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
                f"OpenAI response did not contain a tool_call for "
                f"{tool_name!r}. finish_reason={_get(choice, 'finish_reason')!r}",
                kind="schema_mismatch",
                retryable=False,
                request_id=_get_request_id(raw),
            )

        # OpenAI tool-call arguments are ALWAYS a JSON string.
        args_raw = _get(tool_block, "arguments")
        if isinstance(args_raw, str):
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    f"OpenAI returned non-JSON tool arguments: {args_raw!r}",
                    kind="schema_mismatch",
                    retryable=False,
                    request_id=_get_request_id(raw),
                ) from exc
        elif isinstance(args_raw, Mapping):
            # Defensive — some OpenAI-compatible servers (vLLM, certain
            # proxy setups) hand back already-parsed dicts.
            args = dict(args_raw)
        elif args_raw is None:
            args = {}
        else:
            raise ProviderError(
                f"OpenAI returned tool arguments of unexpected type "
                f"{type(args_raw).__name__}",
                kind="schema_mismatch",
                retryable=False,
                request_id=_get_request_id(raw),
            )

        # Token usage. OpenAI prompt caching surfaces as
        # `usage.prompt_tokens_details.cached_tokens`. We charge the
        # cached portion at the cache-read rate via TokenUsage so the
        # budget tracker undercharges (matches the actual OpenAI bill).
        usage_obj = _get(raw, "usage")
        prompt_tokens = int(_get(usage_obj, "prompt_tokens") or 0) if usage_obj is not None else 0
        completion_tokens = (
            int(_get(usage_obj, "completion_tokens") or 0) if usage_obj is not None else 0
        )
        cached_tokens = 0
        if usage_obj is not None:
            details = _get(usage_obj, "prompt_tokens_details")
            if details is not None:
                cached_tokens = int(_get(details, "cached_tokens") or 0)

        # Subtract cached from prompt to avoid double-charging — the
        # budget tracker bills `input_tokens` at full input price and
        # `cache_read_input_tokens` at the discounted rate.
        usage = TokenUsage(
            input_tokens=max(0, prompt_tokens - cached_tokens),
            output_tokens=completion_tokens,
            cache_read_input_tokens=cached_tokens,
            cache_creation_input_tokens=0,
        )

        return ToolCallResult(
            tool_name=tool_name,
            arguments=args,
            request_id=_get_request_id(raw),
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


def _get_request_id(raw: Any) -> str | None:
    """Extract the OpenAI request id. Newer SDK versions expose it as
    `_request_id` (sourced from the x-request-id response header);
    older / compat versions fall back to `id` (the chat completion
    id, which is good enough for support)."""
    rid = _get(raw, "_request_id")
    if rid is not None:
        return str(rid)
    rid = _get(raw, "id")
    return str(rid) if rid is not None else None


# Type-time Protocol satisfaction check.
_check: LLMProviderClient = OpenAIProviderClient  # type: ignore[assignment, type-abstract]
del _check


__all__ = ("OpenAIProviderClient",)
