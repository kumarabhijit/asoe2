from __future__ import annotations

# Provider factory.
#
# Maps an LLMProvider enum value (from contracts/policy.py) to the
# concrete LLMProviderClient implementation. The constraints layer
# calls `build_provider_client(provider)` and never imports a vendor
# module directly.
#
# Adding a new provider:
#   1. Implement `<Name>ProviderClient` in `llm/<name>_client.py`
#      satisfying the Protocol in `llm/provider_protocol.py`.
#   2. Register it in PROVIDER_FACTORIES below.
#   3. Add the provider key to LLMProvider in contracts/policy.py.
# That's the entire change — orchestration, compliance, recipes,
# tests of the pipeline don't move.

import logging
from typing import Callable

from llm.anthropic_client import AnthropicProviderClient, RemoteLLMConfig
from llm.google_client import GoogleProviderClient
from llm.ollama_client import OllamaProviderClient
from llm.openai_client import OpenAIProviderClient
from llm.provider_protocol import LLMProviderClient, ProviderError

logger = logging.getLogger("asoe.llm.factory")


# Each factory is a function `(RemoteLLMConfig) -> LLMProviderClient`.
# Wrapping in a dict keeps the lookup explicit and makes the registry
# trivial to inspect from tests.
PROVIDER_FACTORIES: dict[str, Callable[[RemoteLLMConfig], LLMProviderClient]] = {
    "anthropic": AnthropicProviderClient.from_config,
    "openai": OpenAIProviderClient.from_config,
    "google": GoogleProviderClient.from_config,
    "ollama": OllamaProviderClient.from_config,
}
"""Registry mapping LLMProvider enum value → factory callable.
The factory takes a RemoteLLMConfig and returns a client implementing
LLMProviderClient. Stub providers (V1: openai/google/ollama) raise
NotImplementedError; the router catches and falls through."""


class UnknownProvider(ValueError):
    """Raised when ASOE_LLM_PROVIDER resolves to a value outside the
    registered set. Distinct from ProviderError because the router
    handles them differently — UnknownProvider triggers a structured
    config-error log; ProviderError triggers the per-call fallback."""


def build_provider_client(provider: str) -> LLMProviderClient:
    """Build a configured LLMProviderClient for `provider`.

    Reads the per-provider env-var prefix via RemoteLLMConfig.from_env.
    Raises:
        UnknownProvider: provider is not a registered key.
        NotImplementedError: provider's client is a V1 stub.
        Other exceptions (ImportError, ValueError, ProductionEgressBlocked,
        RuntimeError for kill switch): propagate so the router can
        log them and fall back to the deterministic backend.
    """
    factory = PROVIDER_FACTORIES.get(provider)
    if factory is None:
        raise UnknownProvider(
            f"Unknown LLM provider {provider!r}; expected one of "
            f"{sorted(PROVIDER_FACTORIES.keys())}."
        )
    config = RemoteLLMConfig.from_env(provider=provider)
    return factory(config)


__all__ = (
    "PROVIDER_FACTORIES",
    "UnknownProvider",
    "build_provider_client",
)
