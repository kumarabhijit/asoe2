from __future__ import annotations

# OllamaProviderClient — Ollama cloud or self-hosted.
#
# Uses the official `ollama` Python SDK (lazy-imported). The SDK's
# `client.chat()` method is OpenAI-style tool-calling capable on
# models that advertise it (Qwen2.5+, Llama 3.1+, Mistral Large 2,
# Mixtral 8x22B, command-r-plus, others).
#
# Three deployment shapes:
#   - Self-hosted, no auth: OLLAMA_BASE_URL=http://host:11434
#     (typical local / Kubernetes deployment)
#   - Self-hosted, bearer token: OLLAMA_BASE_URL + OLLAMA_API_KEY
#     (reverse-proxy adds an Authorization header)
#   - Ollama Cloud (production-blocked unless explicitly opted-in
#     via a base_url that isn't on the public-cloud allowlist)
#
# Cache control: Ollama does NOT expose prompt caching to clients.
# SystemBlock.cache markers are silently ignored — all blocks are
# concatenated into a single system message. cache_read_input_tokens
# and cache_creation_input_tokens stay zero in the returned
# TokenUsage.
#
# Token usage: Ollama returns `prompt_eval_count` (input) and
# `eval_count` (output) in the response. Mapped to
# TokenUsage(input_tokens, output_tokens). Billing-by-token only
# applies when running on Ollama Cloud — self-hosted is "free"
# beyond the GPU bill.
#
# Pricing table: contracts/policy.py LLM_PRICING_USD_PER_M_TOKENS
# only carries Claude entries today. Open-source model entries can
# be added in a follow-up; until then `estimate_cost_usd` returns
# 0.0 for Ollama-served models (with a logged warning at
# configure-time, not per-call). That's the right default for
# self-hosted; Ollama Cloud operators must add pricing entries.

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

logger = logging.getLogger("asoe.llm.ollama")


_OLLAMA_PUBLIC_CLOUD_URLS = frozenset(
    {
        "https://ollama.com",
        "https://api.ollama.com",
        "ollama.com",
        "api.ollama.com",
        "https://ollama.com/",
        "https://api.ollama.com/",
    }
)
"""Public Ollama Cloud URLs blocked in production. Operators must
opt-in to Cloud explicitly by setting OLLAMA_BASE_URL to a private-
peering URL (or accept the egress and override at the policy gate)."""


# Map Ollama / httpx exception class names → ProviderError kinds.
_OLLAMA_KIND_BY_EXC: dict[str, str] = {
    "ResponseError": "server_error",
    "ConnectionError": "connection",
    "ConnectError": "connection",
    "TimeoutException": "timeout",
    "ReadTimeout": "timeout",
    "ConnectTimeout": "timeout",
    "RequestError": "connection",
}


def _classify_ollama_exc(exc: BaseException) -> tuple[str, bool, int | None]:
    """Best-effort classification of an Ollama / httpx exception
    into (kind, retryable, status_code). The Ollama SDK raises
    `ollama.ResponseError` with `.status_code` for HTTP errors and
    re-raises httpx exceptions for network issues."""
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
    kind = _OLLAMA_KIND_BY_EXC.get(name, "unknown")
    retryable = kind in {"timeout", "connection", "server_error", "rate_limit"}
    return kind, retryable, status_code


class OllamaProviderClient:
    """LLMProviderClient implementation backed by the `ollama`
    Python SDK."""

    provider_name: str = "ollama"

    def __init__(self, sdk_client: Any, model_id: str):
        # SDK client is constructed in `from_config()` so tests can
        # inject a fake. We never construct one inline here.
        self._client = sdk_client
        self._model_id = model_id

    @classmethod
    def from_config(cls, config, *, asoe_env: str | None = None) -> "OllamaProviderClient":
        env_resolved = asoe_env if asoe_env is not None else os.getenv("ASOE_ENV", "sandbox")
        if env_resolved == "production" and (
            config.base_url is None or config.base_url in _OLLAMA_PUBLIC_CLOUD_URLS
        ):
            raise RuntimeError(
                "ASOE_ENV=production rejects egress to public Ollama "
                "Cloud. Set OLLAMA_BASE_URL to a self-hosted or "
                "private-peered Ollama endpoint."
            )

        # Defence-in-depth: refuse to open a TCP socket while the
        # kill switch is engaged.
        from hardening.kill_switch import is_kill_switch_active  # noqa: PLC0415

        if is_kill_switch_active():
            raise RuntimeError(
                "ASOE_KILL_SWITCH is active; Ollama client construction "
                "blocked. No outbound TCP opened."
            )

        try:
            from ollama import Client as OllamaClient  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "Ollama SDK is required when ASOE_LLM_PROVIDER=ollama. "
                "Install with: pip install 'asoe[ollama]'"
            ) from exc

        host = config.base_url or "http://localhost:11434"
        # The Ollama SDK accepts `host` (URL with scheme + port) and a
        # generic `headers` dict. Bearer token auth is conventional for
        # Cloud / proxied setups; we attach when api_key is present.
        headers: dict[str, str] = {}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        for header_name, header_val in config.extra_headers:
            headers[header_name] = header_val

        kwargs: dict[str, Any] = {
            "host": host,
            "timeout": config.timeout_s,
        }
        if headers:
            kwargs["headers"] = headers

        sdk_client = OllamaClient(**kwargs)
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
        # Ollama uses OpenAI-style chat messages. Cache markers are
        # ignored — concatenate all system blocks into a single system
        # message.
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
            raw = self._client.chat(
                model=self._model_id,
                messages=messages,
                tools=[tool_def],
                # Ollama doesn't honor OpenAI's `tool_choice` directly;
                # it uses the tools list + the model's own routing. For
                # tool-calling-capable models (Qwen2.5+, Llama 3.1+,
                # Mistral) and a single tool with a forcing description,
                # it picks the tool. If the model refuses, we surface
                # schema_mismatch.
                options={"num_predict": max_tokens, "temperature": 0.0},
                # stream=False is the default; explicit for clarity.
                stream=False,
            )
        except Exception as exc:  # noqa: BLE001
            kind, retryable, status_code = _classify_ollama_exc(exc)
            raise ProviderError(
                f"Ollama call_with_tool failed: {type(exc).__name__}: {exc}",
                kind=kind,
                retryable=retryable,
                status_code=status_code,
                request_id=None,  # Ollama does not surface request ids
            ) from exc

        latency_s = time.monotonic() - started

        # Response is dict-like (typed-dict) or attr-accessible
        # depending on SDK version. Use `.get()` defensively.
        message = _get(raw, "message") or {}
        tool_calls = _get(message, "tool_calls") or []
        tool_block = None
        for tc in tool_calls:
            fn = _get(tc, "function") or {}
            if _get(fn, "name") == tool_name:
                tool_block = fn
                break
        if tool_block is None:
            raise ProviderError(
                f"Ollama response did not contain a tool_call for {tool_name!r}. "
                f"done_reason={_get(raw, 'done_reason')!r}",
                kind="schema_mismatch",
                retryable=False,
            )

        # `arguments` may be a dict OR a JSON string depending on the
        # model and SDK version. Normalise to dict.
        args_raw = _get(tool_block, "arguments")
        if isinstance(args_raw, str):
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    f"Ollama returned non-JSON tool arguments: {args_raw!r}",
                    kind="schema_mismatch",
                    retryable=False,
                ) from exc
        elif isinstance(args_raw, Mapping):
            args = dict(args_raw)
        elif args_raw is None:
            args = {}
        else:
            raise ProviderError(
                f"Ollama returned tool arguments of unexpected type "
                f"{type(args_raw).__name__}",
                kind="schema_mismatch",
                retryable=False,
            )

        usage = TokenUsage(
            input_tokens=int(_get(raw, "prompt_eval_count") or 0),
            output_tokens=int(_get(raw, "eval_count") or 0),
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )

        return ToolCallResult(
            tool_name=tool_name,
            arguments=args,
            request_id=None,
            model_id=str(_get(raw, "model") or self._model_id),
            usage=usage,
            latency_s=latency_s,
            stop_reason=str(_get(raw, "done_reason") or ""),
        )


def _get(obj: Any, key: str) -> Any:
    """Read `key` from an SDK response that may be a dict or a typed
    attr-accessible object."""
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


# Type-time Protocol satisfaction check.
_check: LLMProviderClient = OllamaProviderClient  # type: ignore[assignment, type-abstract]
del _check


__all__ = ("OllamaProviderClient",)
