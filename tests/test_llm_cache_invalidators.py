from __future__ import annotations

# Silent-invalidator audit for LLM prompt caching.
#
# **Panel-blocked test** (Cost/Ops review §7 blocker #3): a single
# `datetime.now()` or `trace_id` interpolated into the system prompt
# would tank the prompt-cache hit rate from ~85% to 0%, moving sandbox
# spend from $5/day to >$50/day silently. The fix is structural (the
# system prompt is composed from static content + alphabetised SKILL.md
# in constraints/llm_backend.py), but only a CI test catches a future
# regression.
#
# The audit:
#   1. Run two graph runs with structurally distinct OrderEvents.
#   2. Assert the rendered system blocks (cache.text + cache.enabled
#      flags) are byte-identical across the two runs.
#   3. Assert the rendered tool_input_schema dict is byte-identical.
#   4. Assert the user_message DIFFERS (otherwise we're not actually
#      rendering the volatile content — false-positive guard).
#
# A failing test means either the system prompt OR the tool schema
# now embeds state-derived data — regression must be fixed before
# the PR can land.

from typing import Any

from constraints.llm_backend import RemoteLLMBackend
from constraints.specs import IntentDecision
from contracts.models import GraphState, Intent, OrderEvent
from llm.budget import InMemoryBudgetTracker
from llm.circuit_breaker import LLMCircuitBreaker
from llm.provider_protocol import (
    SystemBlock,
    TokenUsage,
    ToolCallResult,
)


class CapturingProvider:
    """Records every call_with_tool invocation. Returns a fixed
    valid IntentDecision so the backend's success path runs."""

    provider_name = "fake"

    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def call_with_tool(
        self,
        *,
        system: list[SystemBlock],
        user_message: str,
        tool_name: str,
        tool_description: str,
        tool_input_schema,
        max_tokens: int = 256,
    ) -> ToolCallResult:
        self.calls.append(
            {
                "system_text": tuple(b.text for b in system),
                "system_cache_enabled": tuple(b.cache.enabled for b in system),
                "system_cache_ttl": tuple(b.cache.ttl for b in system),
                "user_message": user_message,
                "tool_name": tool_name,
                "tool_description": tool_description,
                "tool_input_schema": tool_input_schema,
            }
        )
        return ToolCallResult(
            tool_name=tool_name,
            arguments={"intent": "DUPLICATE_PO", "confidence": 0.9},
            request_id="req_test",
            model_id="claude-sonnet-4-6",
            usage=TokenUsage(),
            latency_s=0.1,
            stop_reason="tool_use",
        )


def _backend_with(provider: CapturingProvider) -> RemoteLLMBackend:
    return RemoteLLMBackend(
        provider_client=provider,
        budget=InMemoryBudgetTracker(daily_budget_usd=100.0),
        breaker=LLMCircuitBreaker(),
        skills_dir="skills",
    )


def _state(*, order_id: str, po_price: float = 90.0) -> GraphState:
    return GraphState(
        event=OrderEvent(
            order_id=order_id,
            line_item=1,
            po_price=po_price,
            sap_base_price=100.0,
            retailer_id="R-01",
            line_count=1,
            event_type="EDI_850_DUPLICATE_PO",
        )
    )


# ---------------------------------------------------------------------------
# classify_intent
# ---------------------------------------------------------------------------


def test_classify_system_prefix_byte_identical_across_events() -> None:
    """Two distinct OrderEvents must produce IDENTICAL cacheable
    prefix bytes (system blocks + tool schema). Regression here =
    cache hit rate plummets to 0%."""
    provider = CapturingProvider()
    backend = _backend_with(provider)

    backend.classify_intent(_state(order_id="SO-A", po_price=90.0))
    backend.classify_intent(_state(order_id="SO-B", po_price=200.0))

    a, b = provider.calls
    assert a["system_text"] == b["system_text"], (
        "System block text differs across calls — a state-derived value "
        "leaked into the cacheable prefix. Inspect "
        "constraints/llm_backend.py::_system_blocks_for and the SKILL.md "
        "loader."
    )
    assert a["system_cache_enabled"] == b["system_cache_enabled"]
    assert a["system_cache_ttl"] == b["system_cache_ttl"]
    assert a["tool_input_schema"] == b["tool_input_schema"]
    assert a["tool_description"] == b["tool_description"]
    assert a["tool_name"] == b["tool_name"]


def test_classify_user_message_actually_differs() -> None:
    """False-positive guard: if both sides of the byte-identity
    assertion match because the renderer is broken (always emits
    the same user message), this test catches it."""
    provider = CapturingProvider()
    backend = _backend_with(provider)

    backend.classify_intent(_state(order_id="SO-A", po_price=90.0))
    backend.classify_intent(_state(order_id="SO-B", po_price=200.0))

    a, b = provider.calls
    assert a["user_message"] != b["user_message"]
    assert "SO-A" in a["user_message"]
    assert "SO-B" in b["user_message"]


# ---------------------------------------------------------------------------
# propose_recipe
# ---------------------------------------------------------------------------


def test_recipe_system_prefix_byte_identical_across_events() -> None:
    provider = CapturingProvider()

    # Override the scripted result for a different tool — the
    # CapturingProvider returns IntentDecision args by default; for
    # propose_recipe we'd expect RecipeProposal args. Backend will
    # validation-fail and fall back to deterministic, but the
    # provider IS still called once before the failure — that call
    # is what we audit.
    def call_with_tool(*, system, user_message, tool_name, tool_description,
                       tool_input_schema, max_tokens=256):
        provider.calls.append(
            {
                "system_text": tuple(b.text for b in system),
                "system_cache_enabled": tuple(b.cache.enabled for b in system),
                "system_cache_ttl": tuple(b.cache.ttl for b in system),
                "user_message": user_message,
                "tool_name": tool_name,
                "tool_description": tool_description,
                "tool_input_schema": tool_input_schema,
            }
        )
        return ToolCallResult(
            tool_name=tool_name,
            arguments={"recipe_name": "DuplicatePORecipe.py"},
            request_id="req_test",
            model_id="claude-sonnet-4-6",
            usage=TokenUsage(),
            latency_s=0.1,
            stop_reason="tool_use",
        )
    provider.call_with_tool = call_with_tool  # type: ignore[method-assign]

    backend = _backend_with(provider)

    state_a = _state(order_id="SO-A")
    state_a.intent = Intent.DUPLICATE_PO
    state_b = _state(order_id="SO-B", po_price=200.0)
    state_b.intent = Intent.DUPLICATE_PO

    backend.propose_recipe(state_a)
    backend.propose_recipe(state_b)

    a, b = provider.calls
    assert a["system_text"] == b["system_text"]
    assert a["tool_input_schema"] == b["tool_input_schema"]


# ---------------------------------------------------------------------------
# shadow_decision
# ---------------------------------------------------------------------------


def test_shadow_system_prefix_byte_identical_across_events() -> None:
    provider = CapturingProvider()

    def call_with_tool(*, system, user_message, tool_name, tool_description,
                       tool_input_schema, max_tokens=256):
        provider.calls.append(
            {
                "system_text": tuple(b.text for b in system),
                "system_cache_enabled": tuple(b.cache.enabled for b in system),
                "system_cache_ttl": tuple(b.cache.ttl for b in system),
                "user_message": user_message,
                "tool_name": tool_name,
                "tool_description": tool_description,
                "tool_input_schema": tool_input_schema,
            }
        )
        return ToolCallResult(
            tool_name=tool_name,
            arguments={"status": "GREEN", "reasons": ["ok"], "policy_hits": []},
            request_id="req_test",
            model_id="claude-sonnet-4-6",
            usage=TokenUsage(),
            latency_s=0.1,
            stop_reason="tool_use",
        )
    provider.call_with_tool = call_with_tool  # type: ignore[method-assign]

    backend = _backend_with(provider)

    state_a = _state(order_id="SO-A")
    state_a.intent = Intent.CONTRACTUAL_CORRECTION
    state_b = _state(order_id="SO-B", po_price=200.0)
    state_b.intent = Intent.CONTRACTUAL_CORRECTION

    backend.shadow_decision(state_a)
    backend.shadow_decision(state_b)

    a, b = provider.calls
    assert a["system_text"] == b["system_text"]
    assert a["tool_input_schema"] == b["tool_input_schema"]


# ---------------------------------------------------------------------------
# Tool schemas use sorted keys (cross-pod cache stability)
# ---------------------------------------------------------------------------


def test_tool_schema_keys_are_sorted() -> None:
    """Pydantic's model_json_schema() is stable on a single Python
    version; sorting keys explicitly preserves cross-pod cache hits
    when pods run different builds."""
    provider = CapturingProvider()
    backend = _backend_with(provider)
    backend.classify_intent(_state(order_id="SO-A"))

    schema = provider.calls[0]["tool_input_schema"]
    # Every dict at every depth has keys in sorted order
    def _check_sorted(d):
        if isinstance(d, dict):
            assert list(d.keys()) == sorted(d.keys()), (
                f"unsorted keys: {list(d.keys())}"
            )
            for v in d.values():
                _check_sorted(v)
        elif isinstance(d, list):
            for v in d:
                _check_sorted(v)
    _check_sorted(schema)


# ---------------------------------------------------------------------------
# SKILL.md catalog is loaded into the cacheable system prompt
# ---------------------------------------------------------------------------


def test_skill_catalog_appears_in_first_system_block() -> None:
    """Without the catalog the cacheable prefix is too small (<2048
    tokens on Sonnet 4.6) and prompt caching never activates. The
    catalog injection IS the reason caching works in V1."""
    provider = CapturingProvider()
    backend = _backend_with(provider)
    backend.classify_intent(_state(order_id="SO-A"))

    first_block_text = provider.calls[0]["system_text"][0]
    # Heuristic: at least one of the canonical skill names is in the
    # first block.
    canonical_skill_phrases = [
        "duplicate-po",
        "pricing-reconciliation",
        "edi-mismatch",
        "back-order",
    ]
    assert any(
        phrase in first_block_text.lower() for phrase in canonical_skill_phrases
    ), "SKILL.md catalog appears to be missing from the cacheable prefix"
