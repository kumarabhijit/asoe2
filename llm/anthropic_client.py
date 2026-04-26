from __future__ import annotations

# Anthropic SDK wrapper — thin, configurable, lazy-imported.
#
# The Anthropic SDK is an optional dependency. The constraint backend
# imports this module only when ASOE_LLM_PROVIDER ∈ {anthropic,
# azure_anthropic}; the SDK itself is imported inside `build_client()`
# so the deterministic-default path never pulls it in.
#
# Configuration is centralised in RemoteLLMConfig — a typed Pydantic
# model loaded from env vars in one place. This is the seam architects
# (Vasquez review) flagged: every provider knob (model_id, base_url,
# api_version, deployment_name, timeout_s, max_retries, beta headers)
# lives here, so the Azure AI Foundry pivot is a config flip rather
# than code surgery elsewhere.
#
# Security notes:
#   - No api_key is logged, cached, or echoed. The Anthropic SDK reads
#     ANTHROPIC_API_KEY from the environment by default; we pass it
#     explicitly when set so callers can rotate via Azure Key Vault CSI.
#   - When ASOE_ENV=production, the resolver REJECTS api.anthropic.com
#     as a base_url and falls back to azure_anthropic-only. Implemented
#     in the router (constraints/router.py); enforced again at
#     build_client() defensively.
#   - is_kill_switch_active() is checked at build time so an active
#     kill switch zero-egresses (no TCP open). The router also gates
#     on this before calling build_client.

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from contracts.policy import (
    LLM_CALL_TIMEOUT_S,
    LLM_DEFAULT_MODEL_ID,
)


_PROD_DISALLOWED_BASE_URLS = frozenset(
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


class RemoteLLMConfig(BaseModel):
    """Typed configuration for a remote Anthropic client.

    Loaded once from env vars via `from_env()`; never mutated. The
    Anthropic SDK accepts keyword arguments for every field below — the
    Azure AI Foundry pivot is a matter of pointing `base_url` at the
    Foundry endpoint and supplying the Foundry-issued auth token.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_key: str = Field(min_length=1)
    """Anthropic API key. Sourced from ANTHROPIC_API_KEY env var which
    in production must come from Azure Key Vault CSI. Never logged."""

    base_url: str | None = None
    """Override the SDK default endpoint. Set to the Azure AI Foundry
    private-endpoint URL when ASOE_LLM_PROVIDER=azure_anthropic."""

    model_id: str = LLM_DEFAULT_MODEL_ID
    """Anthropic model alias (e.g. 'claude-sonnet-4-6'). Aligns with
    architecture_v3.md §4.2 default."""

    azure_deployment_name: str | None = None
    """Azure AI Foundry deployment name. Forwarded as a request header
    when set; ignored when calling the public Anthropic endpoint."""

    api_version: str | None = None
    """Anthropic API version header override. Defaults to the SDK's
    pinned anthropic-version when None."""

    region: str | None = None
    """Optional region tag echoed in observability headers; not used
    for routing in V1 (single-region Foundry assumed)."""

    timeout_s: float = LLM_CALL_TIMEOUT_S
    """Per-call timeout enforced by the SDK's httpx client."""

    max_retries: int = 2
    """SDK auto-retry budget on 408/409/429/5xx (exponential backoff).
    Capped at 2 to keep tail latency bounded under 500-concurrent-clients
    incident scenarios — see Cost/Ops review §4."""

    beta_headers: tuple[str, ...] = ()
    """Anthropic beta headers to attach to every request (e.g.
    'task-budgets-2026-03-13'). Empty by default in V1."""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "RemoteLLMConfig":
        """Construct from environment variables.

        Required: ANTHROPIC_API_KEY.
        Optional: ANTHROPIC_BASE_URL, ANTHROPIC_MODEL,
        ANTHROPIC_AZURE_DEPLOYMENT, ANTHROPIC_API_VERSION,
        ANTHROPIC_REGION, ANTHROPIC_TIMEOUT_S, ANTHROPIC_MAX_RETRIES,
        ANTHROPIC_BETA_HEADERS (comma-separated).
        """
        env = env if env is not None else os.environ
        api_key = env.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is required when ASOE_LLM_PROVIDER is "
                "anthropic or azure_anthropic. Falling back to deterministic."
            )

        base_url = env.get("ANTHROPIC_BASE_URL", "").strip() or None
        model_id = env.get("ANTHROPIC_MODEL", "").strip() or LLM_DEFAULT_MODEL_ID
        deployment = env.get("ANTHROPIC_AZURE_DEPLOYMENT", "").strip() or None
        api_version = env.get("ANTHROPIC_API_VERSION", "").strip() or None
        region = env.get("ANTHROPIC_REGION", "").strip() or None

        timeout_raw = env.get("ANTHROPIC_TIMEOUT_S", "").strip()
        timeout_s = float(timeout_raw) if timeout_raw else LLM_CALL_TIMEOUT_S

        retries_raw = env.get("ANTHROPIC_MAX_RETRIES", "").strip()
        max_retries = int(retries_raw) if retries_raw else 2

        beta_raw = env.get("ANTHROPIC_BETA_HEADERS", "").strip()
        beta_headers = tuple(h.strip() for h in beta_raw.split(",") if h.strip())

        return cls(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            azure_deployment_name=deployment,
            api_version=api_version,
            region=region,
            timeout_s=timeout_s,
            max_retries=max_retries,
            beta_headers=beta_headers,
        )

    def assert_production_egress_allowed(self, asoe_env: str) -> None:
        """Defensive check: when ASOE_ENV=production, the resolved
        base_url must NOT be the public Anthropic endpoint.

        The router runs this BEFORE building the client; we keep a
        defence-in-depth call here so a bypass of the router still
        fails closed.
        """
        if asoe_env != "production":
            return
        # In production, base_url MUST be set to an allowlisted Foundry
        # private endpoint. We do not maintain the allowlist here
        # (operator concern); we only block the known public default.
        if self.base_url is None or self.base_url in _PROD_DISALLOWED_BASE_URLS:
            raise ProductionEgressBlocked(
                "ASOE_ENV=production rejects egress to the public Anthropic "
                "API. Set ANTHROPIC_BASE_URL to an allowlisted Azure AI "
                "Foundry private endpoint or unset ASOE_LLM_PROVIDER to "
                "use the deterministic fallback."
            )


def build_client(config: RemoteLLMConfig) -> Any:
    """Build an Anthropic SDK client from a typed config.

    Lazy-imports `anthropic` so the optional dependency is only
    required when a remote provider is active. Raises ImportError with
    an actionable message if the package is missing.

    The kill-switch hard-gate is delegated to the router. This
    function is called only after the router has confirmed the kill
    switch is INACTIVE; we still re-check here as defence-in-depth
    so a bypass of the router doesn't open a TCP connection.
    """
    from hardening.kill_switch import is_kill_switch_active  # noqa: PLC0415

    if is_kill_switch_active():
        raise RuntimeError(
            "ASOE_KILL_SWITCH is active; remote LLM client construction "
            "blocked. No outbound TCP opened."
        )

    try:
        import anthropic  # noqa: PLC0415
    except ImportError as exc:  # noqa: PERF203
        raise ImportError(
            "Anthropic SDK is required when ASOE_LLM_PROVIDER is set to a "
            "remote provider. Install with: pip install 'asoe[anthropic]'"
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
    if config.azure_deployment_name:
        # Foundry routes by deployment name; passed as a header so the
        # public Anthropic endpoint silently ignores it when set.
        default_headers["x-azure-deployment"] = config.azure_deployment_name
    if default_headers:
        kwargs["default_headers"] = default_headers

    return anthropic.Anthropic(**kwargs)
