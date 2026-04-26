from __future__ import annotations

# Provider-agnostic LLM client surface.
#
# Every provider — Anthropic (direct or via Azure AI Foundry), OpenAI
# (direct or Azure OpenAI), Google (Vertex AI / Gemini), Ollama (cloud
# or self-hosted) — implements `LLMProviderClient`. The constraints
# layer (constraints/llm_backend.py) never imports a vendor SDK; it
# only sees this Protocol.
#
# Adding a new provider is a single new file under llm/ that:
#   1. defines a `<Name>ProviderClient` class implementing the Protocol
#   2. is registered in llm/provider_factory.py
#   3. adds a value to LLMProvider in contracts/policy.py
#
# No changes are required in constraints/, orchestration/, or
# compliance/. This is the seam that keeps the architecture
# cloud-portable.
#
# **Tool use is the constrained-output mechanism we standardise on.**
# It maps cleanly to every supported provider's API:
#   - Anthropic: `tools` + `input_schema` + `tool_choice={"type":"tool"}`
#   - OpenAI / Azure OpenAI: `tools` + `function` + `tool_choice={"type":"function"}`
#   - Google Vertex / Gemini: `functionDeclarations` + `toolConfig=ANY`
#   - Ollama (cloud): OpenAI-compatible tool-calling
# All four return a structured arguments dict the provider's SDK has
# already JSON-validated against the schema we supplied.

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Result + error types — strictly provider-agnostic
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheControl:
    """Provider-portable cache-control directive. Each concrete client
    translates `enabled=True` into its provider's prompt-cache marker
    (Anthropic `cache_control: ephemeral`; OpenAI prompt caching is
    automatic on supported models; Google has its own cached-content
    API; Ollama has no concept and ignores)."""

    enabled: bool = False
    ttl: str | None = None  # "5m" / "1h" — Anthropic-only today; ignored elsewhere.


@dataclass(frozen=True)
class SystemBlock:
    """One contiguous chunk of the system prompt. Cacheability is
    declared per block so providers that support prompt caching
    (Anthropic) place breakpoints correctly; providers that don't
    just concatenate the text."""

    text: str
    cache: CacheControl = field(default_factory=CacheControl)


@dataclass(frozen=True)
class TokenUsage:
    """Token accounting reported by the provider. Cache fields are
    zero on providers that don't expose them."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass(frozen=True)
class ToolCallResult:
    """Successful structured tool call from any provider.

    Returned by `LLMProviderClient.call_with_tool()`. The constraints
    backend validates `arguments` against the original Pydantic
    schema (defence-in-depth — the provider has already JSON-validated
    once, but we re-validate to enforce Literal narrowing).
    """

    tool_name: str
    arguments: Mapping[str, Any]
    request_id: str | None
    """Provider request id — Anthropic returns this in the
    `request-id` HTTP header; OpenAI in `x-request-id`; Google
    surfaces it via the SDK. Critical for support tickets."""

    model_id: str
    """Model id actually served (provider may rewrite the requested
    alias). Logged on every TraceRecord — see audit trail."""

    usage: TokenUsage
    latency_s: float
    stop_reason: str | None
    """Provider-native stop reason ('end_turn' / 'tool_use' /
    'stop' / 'length' / etc.). Telemetry only — the constraints
    layer never branches on this."""


class ProviderError(Exception):
    """Base class for provider-side failures the constraints layer
    must handle (timeout, 5xx, rate-limit, schema mismatch, etc.).
    The router catches this and routes the call to the deterministic
    fallback for that single trio method."""

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        retryable: bool = False,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        """Short token: 'timeout' | 'rate_limit' | 'server_error' |
        'auth' | 'schema_mismatch' | 'connection' | 'unknown'.
        Used for Prometheus error_type labels and TraceRecord
        anthropic_error_type."""
        self.retryable = retryable
        self.status_code = status_code
        self.request_id = request_id


# ---------------------------------------------------------------------------
# The Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMProviderClient(Protocol):
    """The single-call surface every provider exposes.

    `call_with_tool()` is the only operation the constraints layer
    needs: invoke the model with a forced tool and return the
    structured arguments. Free-form chat and multi-turn loops are
    out of scope for V1.

    Implementations MUST:
      - never log api_key (defence against accidental secret leakage)
      - raise ProviderError (not bare exceptions) on any failure path
      - include a `request_id` in the result when the provider
        provides one
      - report token usage (zeros on providers that don't surface it)
      - respect `max_tokens` as a hard ceiling
      - apply the provider's prompt-cache marker on cacheable
        SystemBlock entries (ignored on providers that don't support
        caching)
    """

    provider_name: str
    """Provider identifier matching contracts/policy.py LLMProvider:
    'anthropic' | 'openai' | 'google' | 'ollama'. Read-only."""

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
        """Invoke the model with a single forced tool.

        Args:
            system: Ordered cacheable system prompt blocks. The first
                block typically holds the verbatim SKILL.md content;
                breakpoints are auto-placed by the provider client.
            user_message: Per-call volatile content (event payload,
                prior decisions). Goes AFTER the cached prefix.
            tool_name: Name of the tool to force.
            tool_description: Human-readable description.
            tool_input_schema: JSON schema (typically derived from
                a Pydantic model via `.model_json_schema()`). Must be
                deterministic across calls to preserve cache hits.
            max_tokens: Hard ceiling on the model's output tokens.

        Returns:
            ToolCallResult with provider-validated arguments.

        Raises:
            ProviderError: any non-2xx response, timeout, or schema
                mismatch. The constraints layer catches and falls
                through to the deterministic backend.
        """
        ...


__all__ = (
    "CacheControl",
    "LLMProviderClient",
    "ProviderError",
    "SystemBlock",
    "ToolCallResult",
    "TokenUsage",
)
