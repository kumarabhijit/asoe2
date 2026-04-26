from __future__ import annotations

# Stub-provider coverage.
#
# In V1 PR-1, four providers are FULL implementations:
#   - anthropic     (tests/test_anthropic_client.py)
#   - openai        (tests/test_openai_client.py)
#   - ollama        (tests/test_ollama_client.py)
#   - huggingface   (tests/test_huggingface_client.py)
#
# One provider remains a stub:
#   - google
#
# These stub tests pin behavior so:
#   1. Adding a new provider doesn't accidentally drop the prod-
#      egress check.
#   2. The router's fallthrough contract stays explicit.
#   3. Once the stub is implemented, the test file is updated rather
#      than silently passing.

import pytest

from llm import RemoteLLMConfig
from llm.google_client import GoogleProviderClient
from llm.provider_protocol import LLMProviderClient, ProviderError, SystemBlock


def test_stub_satisfies_protocol() -> None:
    """The stub must structurally satisfy the Protocol so the
    factory can return one without a TypeError later."""
    assert GoogleProviderClient.provider_name == "google"
    assert callable(getattr(GoogleProviderClient, "from_config", None))
    assert callable(getattr(GoogleProviderClient, "call_with_tool", None))


def test_google_stub_raises_not_implemented_in_sandbox() -> None:
    cfg = RemoteLLMConfig(api_key="x", project_id="p", region="us-east5")
    with pytest.raises(NotImplementedError, match="V1 stub"):
        GoogleProviderClient.from_config(cfg, asoe_env="sandbox")


# ---------------------------------------------------------------------------
# Production egress gate (must fire BEFORE the NotImplementedError)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blocked",
    [
        None,
        "https://generativelanguage.googleapis.com",
        "https://generativelanguage.googleapis.com/",
    ],
)
def test_google_production_requires_vertex(blocked: str | None) -> None:
    cfg = RemoteLLMConfig(api_key="x", base_url=blocked)
    with pytest.raises(RuntimeError, match="Vertex"):
        GoogleProviderClient.from_config(cfg, asoe_env="production")


# ---------------------------------------------------------------------------
# call_with_tool always raises ProviderError on stubs
# ---------------------------------------------------------------------------


def test_stub_call_raises_provider_error() -> None:
    """Direct stub instances raise ProviderError on call_with_tool —
    the router catches and routes to deterministic without crashing
    the graph."""
    inst = GoogleProviderClient(sdk_client=None, model_id="x")
    with pytest.raises(ProviderError) as ei:
        inst.call_with_tool(
            system=[SystemBlock(text="x")],
            user_message="u",
            tool_name="t",
            tool_description="d",
            tool_input_schema={"type": "object"},
        )
    assert ei.value.kind == "unknown"
    assert ei.value.retryable is False
