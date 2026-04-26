from __future__ import annotations

# Coverage for constraints/llm_backend.py — the provider-agnostic
# constraint backend that wraps any LLMProviderClient.
#
# All tests are network-free: a `FakeProviderClient` replaces the real
# Anthropic/OpenAI/etc. SDK and returns scripted ToolCallResults.
# Budget tracker and circuit breaker are injected per-test so state
# never leaks across cases.

from typing import Any
from unittest import mock

import pytest

from constraints.fallback_backend import DeterministicFallbackBackend
from constraints.llm_backend import RemoteLLMBackend
from constraints.specs import (
    IntentDecision,
    RecipeProposal,
    ShadowDecisionSchema,
)
from contracts.models import GraphState, Intent, OrderEvent
from llm.budget import InMemoryBudgetTracker
from llm.circuit_breaker import LLMCircuitBreaker
from llm.provider_protocol import (
    ProviderError,
    SystemBlock,
    TokenUsage,
    ToolCallResult,
)


# ---------------------------------------------------------------------------
# Fake provider — captures call inputs, returns scripted outputs
# ---------------------------------------------------------------------------


class FakeProviderClient:
    provider_name = "fake"

    def __init__(self, scripted_result: ToolCallResult | Exception):
        self.calls: list[dict[str, Any]] = []
        self._scripted = scripted_result

    def call_with_tool(
        self,
        *,
        system,
        user_message,
        tool_name,
        tool_description,
        tool_input_schema,
        max_tokens,
    ):
        self.calls.append(
            {
                "system": system,
                "user_message": user_message,
                "tool_name": tool_name,
                "tool_description": tool_description,
                "tool_input_schema": tool_input_schema,
                "max_tokens": max_tokens,
            }
        )
        if isinstance(self._scripted, Exception):
            raise self._scripted
        return self._scripted


def _ok_result(tool_name: str, args: dict, *, model_id: str = "claude-sonnet-4-6") -> ToolCallResult:
    return ToolCallResult(
        tool_name=tool_name,
        arguments=args,
        request_id="req_test_123",
        model_id=model_id,
        usage=TokenUsage(
            input_tokens=120,
            output_tokens=15,
            cache_read_input_tokens=6000,
            cache_creation_input_tokens=0,
        ),
        latency_s=0.42,
        stop_reason="tool_use",
    )


def _make_backend(
    *,
    scripted: ToolCallResult | Exception,
    budget_usd: float = 5.0,
) -> tuple[RemoteLLMBackend, FakeProviderClient, InMemoryBudgetTracker, LLMCircuitBreaker]:
    provider = FakeProviderClient(scripted)
    budget = InMemoryBudgetTracker(daily_budget_usd=budget_usd)
    breaker = LLMCircuitBreaker()
    backend = RemoteLLMBackend(
        provider_client=provider,
        budget=budget,
        breaker=breaker,
        # Use the real skills/ catalog so we exercise the full prompt
        # assembly path, but we don't assert on its contents.
        skills_dir="skills",
    )
    return backend, provider, budget, breaker


def _pricing_state() -> GraphState:
    return GraphState(
        event=OrderEvent(
            order_id="SO-TEST",
            line_item=1,
            po_price=90.0,
            sap_base_price=100.0,
            retailer_id="R-01",
            line_count=1,
        )
    )


# ---------------------------------------------------------------------------
# classify_intent — happy + fallback paths
# ---------------------------------------------------------------------------


def test_classify_intent_returns_validated_decision() -> None:
    backend, provider, _, breaker = _make_backend(
        scripted=_ok_result(
            "classify_intent",
            {"intent": "DUPLICATE_PO", "confidence": 0.92},
        ),
    )
    state = _pricing_state()
    state.event.event_type = "EDI_850_DUPLICATE_PO"

    decision = backend.classify_intent(state)
    assert isinstance(decision, IntentDecision)
    assert decision.intent == "DUPLICATE_PO"
    assert decision.confidence == pytest.approx(0.92)

    # Provider was actually called
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["tool_name"] == "classify_intent"
    # Schema came from Pydantic
    assert call["tool_input_schema"]["type"] == "object"
    # Two cacheable system blocks
    assert len(call["system"]) == 2
    for block in call["system"]:
        assert isinstance(block, SystemBlock)
        assert block.cache.enabled is True


def test_classify_intent_falls_back_on_provider_error() -> None:
    err = ProviderError("upstream 500", kind="server_error", retryable=True)
    backend, provider, _, breaker = _make_backend(scripted=err)

    state = _pricing_state()
    decision = backend.classify_intent(state)
    # Fallback is the deterministic backend — it always returns an
    # IntentDecision (CONTRACTUAL_CORRECTION for plain pricing events).
    assert isinstance(decision, IntentDecision)
    assert decision.intent in {
        "CONTRACTUAL_CORRECTION",
        "MASS_PRICING_ERROR",
        "CREDIT_BLOCK",
        "DUPLICATE_PO",
        "PRICE_HOLD_RELEASE",
        "EDI_MISMATCH",
        "BACK_ORDER",
        "OVER_MAX",
        "MIN_ORDER_QTY",
        "PALLET_CONFIG",
        "DELIVERY_DELAY",
    }
    # Breaker should record a failure
    assert breaker.snapshot().error_rate > 0


def test_classify_intent_falls_back_on_validation_error() -> None:
    """Provider returns a value outside the AllowedIntent enum →
    Pydantic validation fails → backend serves the deterministic
    answer."""
    backend, _, _, _ = _make_backend(
        scripted=_ok_result(
            "classify_intent",
            {"intent": "BOGUS_INTENT_NOT_IN_ENUM", "confidence": 0.9},
        ),
    )
    state = _pricing_state()
    decision = backend.classify_intent(state)
    assert isinstance(decision, IntentDecision)
    # The deterministic answer for a plain pricing event is
    # CONTRACTUAL_CORRECTION.
    assert decision.intent == "CONTRACTUAL_CORRECTION"


# ---------------------------------------------------------------------------
# Budget hard-block short-circuits BEFORE the provider call
# ---------------------------------------------------------------------------


def test_budget_hard_block_short_circuits_to_fallback() -> None:
    backend, provider, budget, _ = _make_backend(
        scripted=_ok_result("classify_intent", {"intent": "DUPLICATE_PO", "confidence": 0.9}),
        budget_usd=1.0,
    )
    # Burn the entire budget before any LLM call
    budget.consume(1.5)
    state = _pricing_state()

    decision = backend.classify_intent(state)
    assert isinstance(decision, IntentDecision)
    # Provider was NOT called — hard-block triggered first
    assert provider.calls == []


# ---------------------------------------------------------------------------
# Circuit breaker short-circuits BEFORE the provider call
# ---------------------------------------------------------------------------


def test_open_breaker_short_circuits_to_fallback() -> None:
    backend, provider, _, breaker = _make_backend(
        scripted=_ok_result("classify_intent", {"intent": "DUPLICATE_PO", "confidence": 0.9}),
    )
    # Trip the breaker
    for _ in range(10):
        breaker.record_failure(0.1)

    state = _pricing_state()
    decision = backend.classify_intent(state)
    assert isinstance(decision, IntentDecision)
    assert provider.calls == []  # bypassed


# ---------------------------------------------------------------------------
# propose_recipe — happy + MASS_PRICING_ERROR short-circuit
# ---------------------------------------------------------------------------


def test_propose_recipe_returns_validated_proposal() -> None:
    backend, provider, _, _ = _make_backend(
        scripted=_ok_result(
            "propose_recipe",
            {"recipe_name": "DuplicatePORecipe.py"},
        ),
    )
    state = _pricing_state()
    state.intent = Intent.DUPLICATE_PO

    proposal = backend.propose_recipe(state)
    assert isinstance(proposal, RecipeProposal)
    assert proposal.recipe_name == "DuplicatePORecipe.py"
    assert len(provider.calls) == 1


def test_propose_recipe_short_circuits_for_mass_pricing_error() -> None:
    """MASS_PRICING_ERROR has no recipe by design — backend must
    return None WITHOUT calling the provider (saves cost)."""
    backend, provider, _, _ = _make_backend(
        scripted=_ok_result(
            "propose_recipe",
            {"recipe_name": "DuplicatePORecipe.py"},  # would be wrong
        ),
    )
    state = _pricing_state()
    state.intent = Intent.MASS_PRICING_ERROR

    proposal = backend.propose_recipe(state)
    assert proposal is None
    assert provider.calls == []  # zero LLM cost


def test_propose_recipe_falls_back_on_provider_error() -> None:
    backend, _, _, _ = _make_backend(
        scripted=ProviderError("rate limited", kind="rate_limit", retryable=True),
    )
    state = _pricing_state()
    state.intent = Intent.DUPLICATE_PO
    proposal = backend.propose_recipe(state)
    # Deterministic fallback returns the right recipe
    assert isinstance(proposal, RecipeProposal)
    assert proposal.recipe_name == "DuplicatePORecipe.py"


# ---------------------------------------------------------------------------
# shadow_decision — happy + fallback paths
# ---------------------------------------------------------------------------


def test_shadow_decision_returns_validated_verdict() -> None:
    backend, provider, _, _ = _make_backend(
        scripted=_ok_result(
            "shadow_decision",
            {"status": "GREEN", "reasons": ["ok"], "policy_hits": []},
        ),
    )
    state = _pricing_state()
    state.intent = Intent.CONTRACTUAL_CORRECTION

    verdict = backend.shadow_decision(state)
    assert isinstance(verdict, ShadowDecisionSchema)
    assert verdict.status == "GREEN"
    assert verdict.reasons == ["ok"]


def test_shadow_decision_falls_back_on_validation_error() -> None:
    backend, _, _, _ = _make_backend(
        scripted=_ok_result(
            "shadow_decision",
            {"status": "MAGENTA", "reasons": ["??"], "policy_hits": []},
        ),
    )
    state = _pricing_state()
    state.intent = Intent.CONTRACTUAL_CORRECTION
    verdict = backend.shadow_decision(state)
    # Falls back to deterministic shadow which returns GREEN for a
    # plain CONTRACTUAL_CORRECTION case with no risk signals.
    assert isinstance(verdict, ShadowDecisionSchema)
    assert verdict.status in {"GREEN", "YELLOW", "RED"}


# ---------------------------------------------------------------------------
# Cost accounting — budget consumed AFTER call with measured tokens
# ---------------------------------------------------------------------------


def test_budget_consumed_after_successful_call() -> None:
    backend, _, budget, _ = _make_backend(
        scripted=_ok_result(
            "classify_intent",
            {"intent": "DUPLICATE_PO", "confidence": 0.9},
            model_id="claude-sonnet-4-6",
        ),
    )
    state = _pricing_state()
    state.event.event_type = "EDI_850_DUPLICATE_PO"
    backend.classify_intent(state)

    snap = budget.snapshot()
    # Some cost was consumed (>0)
    assert snap.consumed_usd > 0


def test_unknown_model_does_not_consume_budget() -> None:
    """estimate_cost_usd returns 0.0 for unknown model_ids — so no
    budget consumed, but the call still succeeds (operator sees the
    pricing-table warning in logs)."""
    backend, _, budget, _ = _make_backend(
        scripted=_ok_result(
            "classify_intent",
            {"intent": "DUPLICATE_PO", "confidence": 0.9},
            model_id="future-model-not-in-pricing-table",
        ),
    )
    state = _pricing_state()
    state.event.event_type = "EDI_850_DUPLICATE_PO"
    backend.classify_intent(state)
    assert budget.snapshot().consumed_usd == 0.0


# ---------------------------------------------------------------------------
# Determinism — same event → same rendered system+tools prefix
# ---------------------------------------------------------------------------


def test_system_prompt_is_deterministic_across_calls() -> None:
    """The cacheable prefix (system blocks + tool schema) must be
    byte-identical for two distinct OrderEvents — otherwise prompt
    caching never hits and the panel-blocked cost target is missed."""
    backend, provider, _, _ = _make_backend(
        scripted=_ok_result("classify_intent", {"intent": "DUPLICATE_PO", "confidence": 0.9}),
    )

    state_a = _pricing_state()
    state_a.event.order_id = "SO-A"
    state_b = _pricing_state()
    state_b.event.order_id = "SO-B"
    state_b.event.po_price = 200.0

    backend.classify_intent(state_a)
    backend.classify_intent(state_b)

    assert len(provider.calls) == 2
    # System blocks identical
    a, b = provider.calls
    assert [s.text for s in a["system"]] == [s.text for s in b["system"]]
    assert [s.cache.enabled for s in a["system"]] == [s.cache.enabled for s in b["system"]]
    # Tool schema identical
    assert a["tool_input_schema"] == b["tool_input_schema"]
    # User messages DIFFER (order_id, po_price differ)
    assert a["user_message"] != b["user_message"]


# ---------------------------------------------------------------------------
# Custom fallback — operators can inject their own
# ---------------------------------------------------------------------------


def test_custom_fallback_is_used() -> None:
    custom_fallback = mock.Mock(spec=DeterministicFallbackBackend)
    custom_fallback.classify_intent.return_value = IntentDecision(
        intent="DUPLICATE_PO",
        confidence=0.5,
        rationale="custom-fallback",
    )

    provider = FakeProviderClient(
        ProviderError("boom", kind="server_error", retryable=False)
    )
    backend = RemoteLLMBackend(
        provider_client=provider,
        fallback_backend=custom_fallback,
        budget=InMemoryBudgetTracker(),
        breaker=LLMCircuitBreaker(),
    )
    state = _pricing_state()
    decision = backend.classify_intent(state)
    assert decision.rationale == "custom-fallback"
    custom_fallback.classify_intent.assert_called_once_with(state)
