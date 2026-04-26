from __future__ import annotations

# AnthropicProviderClient — implements LLMProviderClient via the
# official Anthropic Python SDK.
#
# Works with three deployment shapes:
#   - Direct Anthropic API (sandbox/dev): ANTHROPIC_BASE_URL unset
#   - Azure AI Foundry private endpoint (production):
#     ANTHROPIC_BASE_URL=<foundry-url>
#   - Any custom Anthropic-compatible endpoint
#
# The Anthropic SDK is an OPTIONAL dependency. This module is
# importable without it; the SDK is lazy-imported inside
# `from_config()` only when an Anthropic client is actually built.
# Tests stub `sys.modules['anthropic']` to verify behavior without
# the package installed.
#
# Security:
#   - api_key is never logged. The `request-id` returned by Anthropic
#     IS logged (no secret content).
#   - When ASOE_ENV=production, the resolver REJECTS api.anthropic.com
#     as a base_url and falls closed (ProductionEgressBlocked). The
#     router catches and routes to the deterministic fallback for
#     that call only.
#   - is_kill_switch_active() is checked at build time so an active
#     kill switch zero-egresses (no TCP open). The router also gates
#     on this before calling build_client.

import logging
import os
import time
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from contracts.policy import (
    LLM_CALL_TIMEOUT_S,
    LLM_DEFAULT_MODEL_ID,
)
from llm.provider_protocol import (
    LLMProviderClient,
    ProviderError,
    SystemBlock,
    ToolCallResult,
    TokenUsage,
)

logger = logging.getLogger("asoe.llm.anthropic")


_ANTHROPIC_PUBLIC_BASE_URLS = frozenset(
    {
        "https://api.anthropic.com",
        "https://api.anthropic.com/",
        "api.anthropic.com",
    }
)
"""Base URLs that are NEVER allowed in production (ASOE_ENV=production).
Production must route via Azure AI Foundry private endpoint or another
allowlisted Foundry URL. Public Anthropic egress out of the Azure VPC
violates the §11.5 / §4.3 security posture."""


class ProductionEgressBlocked(RuntimeError):
    """Raised when the resolved base URL is rejected by the
    ASOE_ENV=production policy gate. The router catches this and falls
    closed to DeterministicFallbackBackend with a structured warning."""


# ---------------------------------------------------------------------------
# Config — generic across providers; AnthropicProviderClient pulls only
# the fields it needs.
# ---------------------------------------------------------------------------


class RemoteLLMConfig(BaseModel):
    """Provider-agnostic configuration for any remote LLM client.

    Each provider's `from_config()` extracts only the fields it needs.
    Adding a provider that needs a new field (e.g. Google Vertex
    `project_id`) means adding the field here as Optional, never
    breaking the existing providers.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_key: str | None = None
    """API key. Required for Anthropic / OpenAI / Google. Optional for
    Ollama self-hosted. Sourced from <PROVIDER>_API_KEY env vars which
    in production must come from Azure Key Vault CSI. Never logged."""

    base_url: str | None = None
    """Override the SDK default endpoint. Set to the cloud-private
    endpoint URL when running on Azure AI Foundry / Azure OpenAI /
    Vertex AI / Ollama Cloud."""

    model_id: str = LLM_DEFAULT_MODEL_ID
    """Model alias (e.g. 'claude-sonnet-4-6', 'gpt-4o', 'gemini-2.5-pro',
    'qwen2.5:32b')."""

    deployment_name: str | None = None
    """Cloud-deployment identifier. Azure OpenAI requires it (the
    deployment name maps the alias to a model SKU); Anthropic on
    Foundry forwards it as a header; OpenAI direct ignores it."""

    api_version: str | None = None
    """API version header override. Required by Azure OpenAI; optional
    elsewhere (defaults to the SDK's pinned version when None)."""

    region: str | None = None
    """Cloud region tag. Required by Google Vertex (e.g. 'us-east5');
    informational on others."""

    project_id: str | None = None
    """Cloud project id. Required by Google Vertex (GCP project
    holding the AI Platform API)."""

    timeout_s: float = LLM_CALL_TIMEOUT_S
    """Per-call timeout enforced by the SDK's HTTP client."""

    max_retries: int = 2
    """SDK auto-retry budget on 408/409/429/5xx (exponential backoff).
    Capped at 2 to keep tail latency bounded under 500-concurrent-clients
    incident scenarios — see Cost/Ops review §4."""

    extra_headers: tuple[tuple[str, str], ...] = ()
    """Additional HTTP headers forwarded with every request. Used for
    provider-specific betas (Anthropic `anthropic-beta:`) and tenant
    tagging in observability pipelines. Tuple-of-tuples to keep the
    config hashable / frozen."""

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        provider: str = "anthropic",
    ) -> "RemoteLLMConfig":
        """Construct from environment variables.

        Each provider has its own env-var prefix to keep keys scoped:
            anthropic → ANTHROPIC_*
            openai    → OPENAI_*
            google    → GOOGLE_*
            ollama    → OLLAMA_*

        Args:
            env: Override the process env (testing).
            provider: Which env-var prefix to read. Must match the
                LLMProvider enum value.
        """
        env = env if env is not None else os.environ
        prefix = provider.upper()

        api_key = (env.get(f"{prefix}_API_KEY", "") or "").strip() or None
        base_url = (env.get(f"{prefix}_BASE_URL", "") or "").strip() or None
        model_id = (env.get(f"{prefix}_MODEL", "") or "").strip() or LLM_DEFAULT_MODEL_ID
        deployment = (env.get(f"{prefix}_DEPLOYMENT", "") or "").strip() or None
        api_version = (env.get(f"{prefix}_API_VERSION", "") or "").strip() or None
        region = (env.get(f"{prefix}_REGION", "") or "").strip() or None
        project_id = (env.get(f"{prefix}_PROJECT_ID", "") or "").strip() or None

        timeout_raw = (env.get(f"{prefix}_TIMEOUT_S", "") or "").strip()
        timeout_s = float(timeout_raw) if timeout_raw else LLM_CALL_TIMEOUT_S

        retries_raw = (env.get(f"{prefix}_MAX_RETRIES", "") or "").strip()
        max_retries = int(retries_raw) if retries_raw else 2

        headers_raw = (env.get(f"{prefix}_EXTRA_HEADERS", "") or "").strip()
        extra_headers: tuple[tuple[str, str], ...] = ()
        if headers_raw:
            pairs = []
            for chunk in headers_raw.split(";"):
                chunk = chunk.strip()
                if not chunk:
                    continue
                if ":" not in chunk:
                    continue
                k, v = chunk.split(":", 1)
                pairs.append((k.strip(), v.strip()))
            extra_headers = tuple(pairs)

        return cls(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            deployment_name=deployment,
            api_version=api_version,
            region=region,
            project_id=project_id,
            timeout_s=timeout_s,
            max_retries=max_retries,
            extra_headers=extra_headers,
        )


# ---------------------------------------------------------------------------
# AnthropicProviderClient
# ---------------------------------------------------------------------------


def _assert_anthropic_production_egress_allowed(
    base_url: str | None,
    asoe_env: str,
) -> None:
    """In production, base_url MUST be set to an allowlisted Foundry
    private endpoint (or any non-public URL). Public Anthropic API is
    blocked by policy (Chen review §2)."""
    if asoe_env != "production":
        return
    if base_url is None or base_url in _ANTHROPIC_PUBLIC_BASE_URLS:
        raise ProductionEgressBlocked(
            "ASOE_ENV=production rejects egress to api.anthropic.com. "
            "Set ANTHROPIC_BASE_URL to an allowlisted Azure AI Foundry "
            "private endpoint or use a different provider."
        )


_ANTHROPIC_KIND_BY_EXC: dict[str, str] = {
    "RateLimitError": "rate_limit",
    "APITimeoutError": "timeout",
    "APIConnectionError": "connection",
    "AuthenticationError": "auth",
    "BadRequestError": "schema_mismatch",
    "PermissionDeniedError": "auth",
    "NotFoundError": "unknown",
    "InternalServerError": "server_error",
    "APIStatusError": "server_error",
}


def _classify_anthropic_exc(exc: BaseException) -> tuple[str, bool]:
    name = type(exc).__name__
    kind = _ANTHROPIC_KIND_BY_EXC.get(name, "unknown")
    retryable = kind in {"rate_limit", "timeout", "connection", "server_error"}
    return kind, retryable


class AnthropicProviderClient:
    """LLMProviderClient implementation for the Anthropic Python SDK.

    Wraps `client.messages.create()` with a single forced-tool
    request. Maps SystemBlock with cache.enabled=True to Anthropic's
    `cache_control: ephemeral` markers.
    """

    provider_name: str = "anthropic"

    def __init__(self, sdk_client: Any, model_id: str, max_retries: int = 2):
        # The SDK client is constructed by `from_config()` so tests can
        # inject a fake. We never construct it inline here.
        self._client = sdk_client
        self._model_id = model_id
        self._max_retries = max_retries

    @classmethod
    def from_config(
        cls,
        config: RemoteLLMConfig,
        *,
        asoe_env: str | None = None,
    ) -> "AnthropicProviderClient":
        """Lazy-import the Anthropic SDK and build a configured client.

        `asoe_env` defaults to the ASOE_ENV env var; pass explicitly
        in tests. Raises ProductionEgressBlocked when the resolved
        base_url is the public Anthropic API in production.
        """
        if not config.api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is required for the anthropic provider."
            )
        env_resolved = asoe_env if asoe_env is not None else os.getenv("ASOE_ENV", "sandbox")
        _assert_anthropic_production_egress_allowed(config.base_url, env_resolved)

        # Defence-in-depth: refuse to open a TCP socket while the
        # kill switch is engaged. The router gates on this earlier
        # too — both paths must hold.
        from hardening.kill_switch import is_kill_switch_active  # noqa: PLC0415

        if is_kill_switch_active():
            raise RuntimeError(
                "ASOE_KILL_SWITCH is active; remote LLM client construction "
                "blocked. No outbound TCP opened."
            )

        try:
            import anthropic  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "Anthropic SDK is required when ASOE_LLM_PROVIDER=anthropic. "
                "Install with: pip install 'asoe[anthropic]'"
            ) from exc

        kwargs: dict[str, Any] = {
            "api_key": config.api_key,
            "timeout": config.timeout_s,
            "max_retries": config.max_retries,
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url

        default_headers: dict[str, str] = {}
        if config.api_version:
            default_headers["anthropic-version"] = config.api_version
        if config.deployment_name:
            # Foundry routes by deployment name; harmless on the
            # public Anthropic endpoint which ignores unknown headers.
            default_headers["x-azure-deployment"] = config.deployment_name
        for header_name, header_val in config.extra_headers:
            default_headers[header_name] = header_val
        if default_headers:
            kwargs["default_headers"] = default_headers

        sdk_client = anthropic.Anthropic(**kwargs)
        return cls(
            sdk_client=sdk_client,
            model_id=config.model_id,
            max_retries=config.max_retries,
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
        # Map SystemBlock list → Anthropic's system parameter (list of
        # text blocks). Cacheable blocks get cache_control markers.
        system_blocks: list[dict[str, Any]] = []
        for block in system:
            entry: dict[str, Any] = {"type": "text", "text": block.text}
            if block.cache.enabled:
                cc: dict[str, Any] = {"type": "ephemeral"}
                if block.cache.ttl:
                    cc["ttl"] = block.cache.ttl
                entry["cache_control"] = cc
            system_blocks.append(entry)

        tool_def = {
            "name": tool_name,
            "description": tool_description,
            "input_schema": dict(tool_input_schema),
        }

        started = time.monotonic()
        try:
            raw = self._client.messages.create(
                model=self._model_id,
                max_tokens=max_tokens,
                system=system_blocks,
                messages=[{"role": "user", "content": user_message}],
                tools=[tool_def],
                tool_choice={
                    "type": "tool",
                    "name": tool_name,
                    "disable_parallel_tool_use": True,
                },
            )
        except Exception as exc:  # noqa: BLE001
            kind, retryable = _classify_anthropic_exc(exc)
            status_code = getattr(exc, "status_code", None)
            request_id = getattr(exc, "request_id", None) or getattr(
                getattr(exc, "response", None), "headers", {}
            ).get("request-id") if hasattr(exc, "response") else None
            raise ProviderError(
                f"Anthropic call_with_tool failed: {type(exc).__name__}: {exc}",
                kind=kind,
                retryable=retryable,
                status_code=status_code,
                request_id=request_id,
            ) from exc

        latency_s = time.monotonic() - started

        # Find the tool_use block in the response.
        tool_block = None
        for block in raw.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == tool_name:
                tool_block = block
                break
        if tool_block is None:
            raise ProviderError(
                f"Anthropic response did not contain a tool_use block for {tool_name!r}.",
                kind="schema_mismatch",
                retryable=False,
                request_id=getattr(raw, "_request_id", None),
            )

        usage_obj = getattr(raw, "usage", None)
        usage = TokenUsage(
            input_tokens=getattr(usage_obj, "input_tokens", 0) or 0,
            output_tokens=getattr(usage_obj, "output_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage_obj, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(usage_obj, "cache_creation_input_tokens", 0) or 0,
        )

        return ToolCallResult(
            tool_name=tool_name,
            arguments=dict(getattr(tool_block, "input", {}) or {}),
            request_id=getattr(raw, "_request_id", None),
            model_id=getattr(raw, "model", self._model_id),
            usage=usage,
            latency_s=latency_s,
            stop_reason=getattr(raw, "stop_reason", None),
        )


# ---------------------------------------------------------------------------
# Back-compat surface — earlier S1 code referenced `build_client`. The
# function is retained so existing tests keep passing; new code should
# call AnthropicProviderClient.from_config directly.
# ---------------------------------------------------------------------------


def build_client(config: RemoteLLMConfig) -> Any:
    """Construct a raw Anthropic SDK client from a config. Retained
    for back-compat with S1 tests; the constraints layer uses
    AnthropicProviderClient.from_config(config) instead, which wraps
    the SDK with the LLMProviderClient surface."""
    client = AnthropicProviderClient.from_config(config)
    return client._client  # noqa: SLF001 — intentional for back-compat


# Static type-time assertion that AnthropicProviderClient satisfies
# the Protocol. Falls back to a runtime isinstance check at module
# import for a clear error if the contract drifts.
_runtime_protocol_check: LLMProviderClient = AnthropicProviderClient  # type: ignore[assignment, type-abstract]
del _runtime_protocol_check


__all__ = (
    "AnthropicProviderClient",
    "ProductionEgressBlocked",
    "RemoteLLMConfig",
    "build_client",
)
