from __future__ import annotations

# S5a coverage — LLM provenance telemetry end-to-end:
#   - LLMCallTrace schema + GraphState wiring
#   - RemoteLLMBackend.last_call_trace populated on every exit
#     branch (success, ProviderError, CircuitOpen, budget hard-block,
#     validation error)
#   - Orchestration nodes drain last_call_trace onto state
#   - Cross-check disagreement signal stamped onto the trace
#   - Tracer.build_record aggregates token counts + cost + flags
#
# Network-free: provider client is a fake; budget + breaker injected.

from typing import Any
from unittest import mock

import pytest

from constraints.fallback_backend import DeterministicFallbackBackend
from constraints.llm_backend import RemoteLLMBackend
from constraints.specs import IntentDecision
from contracts.models import GraphState, Intent, LLMCallTrace, OrderEvent, TerminalStatus
from llm.budget import InMemoryBudgetTracker
from llm.circuit_breaker import LLMCircuitBreaker
from llm.provider_protocol import (
    ProviderError,
    SystemBlock,
    TokenUsage,
    ToolCallResult,
)
from observability.tracer import Tracer
from orchestration.nodes import (
    _reset_backend_cache,
    classify,
    select_recipe,
    shadow_audit,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


class _Provider:
    provider_name = "anthropic"

    def __init__(self, scripted: ToolCallResult | Exception):
        self._scripted = scripted

    def call_with_tool(self, **kwargs: Any) -> ToolCallResult:
        if isinstance(self._scripted, Exception):
            raise self._scripted
        return self._scripted


def _ok_result(args: dict, *, model_id: str = "claude-sonnet-4-6") -> ToolCallResult:
    return ToolCallResult(
        tool_name=next(iter(args.get("__tool", "classify_intent").split())) or "classify_intent",
        arguments=args,
        request_id="req_test_xyz",
        model_id=model_id,
        usage=TokenUsage(
            input_tokens=500,
            output_tokens=80,
            cache_read_input_tokens=6000,
            cache_creation_input_tokens=0,
        ),
        latency_s=0.42,
        stop_reason="tool_use",
    )


def _build_backend(provider: _Provider, *, budget_usd: float = 5.0):
    return RemoteLLMBackend(
        provider_client=provider,
        budget=InMemoryBudgetTracker(daily_budget_usd=budget_usd),
        breaker=LLMCircuitBreaker(),
        skills_dir="skills",
    )


def _state(*, order_id: str = "SO-T", po_price: float = 90.0) -> GraphState:
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
# RemoteLLMBackend.last_call_trace
# ---------------------------------------------------------------------------


def test_success_populates_trace_with_token_usage_and_hashes() -> None:
    p = _Provider(_ok_result({
        "intent": "DUPLICATE_PO", "confidence": 0.92,
    }))
    backend = _build_backend(p)
    state = _state()
    backend.classify_intent(state)

    trace = backend.last_call_trace
    assert trace is not None
    assert trace.task == "intent"
    assert trace.provider == "anthropic"
    assert trace.model_id == "claude-sonnet-4-6"
    assert trace.request_id == "req_test_xyz"
    assert trace.input_tokens == 500
    assert trace.output_tokens == 80
    assert trace.cache_read_input_tokens == 6000
    assert trace.cache_creation_input_tokens == 0
    assert trace.cost_usd_estimate > 0
    assert trace.stop_reason == "tool_use"
    assert trace.latency_ms == 420  # 0.42 * 1000
    assert trace.fallback_to_deterministic is False
    assert trace.fallback_reason is None
    # Hashes set + non-empty
    assert len(trace.prompt_hash) == 64
    assert len(trace.tool_call_hash) == 64
    assert len(trace.skill_md_version) == 64


def test_provider_error_populates_fallback_trace() -> None:
    err = ProviderError("rate limited", kind="rate_limit", retryable=True)
    p = _Provider(err)
    backend = _build_backend(p)
    state = _state()
    backend.classify_intent(state)

    trace = backend.last_call_trace
    assert trace is not None
    assert trace.fallback_to_deterministic is True
    assert trace.fallback_reason == "rate_limit"
    # Fallback path: token counts stay zero
    assert trace.input_tokens == 0
    assert trace.output_tokens == 0
    assert trace.cost_usd_estimate == 0.0
    # Prompt hash still set so cross-pod cache analysis works
    assert len(trace.prompt_hash) == 64


def test_circuit_open_populates_fallback_trace() -> None:
    p = _Provider(_ok_result({"intent": "DUPLICATE_PO", "confidence": 0.9}))
    backend = _build_backend(p)
    # Trip the breaker before the call
    for _ in range(10):
        backend._breaker.record_failure(0.1)  # noqa: SLF001
    backend.classify_intent(_state())

    trace = backend.last_call_trace
    assert trace is not None
    assert trace.fallback_reason == "circuit_open"


def test_budget_hard_block_populates_fallback_trace() -> None:
    p = _Provider(_ok_result({"intent": "DUPLICATE_PO", "confidence": 0.9}))
    backend = _build_backend(p, budget_usd=1.0)
    backend._budget.consume(1.5)  # noqa: SLF001 — burn the budget

    backend.classify_intent(_state())
    trace = backend.last_call_trace
    assert trace is not None
    assert trace.fallback_reason == "budget_hard_block"


def test_validation_error_populates_trace_with_tokens_and_fallback() -> None:
    """Provider returned successfully but Pydantic rejected the
    arguments. Tokens DID burn on the provider side, so trace records
    them, AND fallback flag is set."""
    p = _Provider(_ok_result({"intent": "BOGUS_INTENT", "confidence": 0.9}))
    backend = _build_backend(p)
    backend.classify_intent(_state())

    trace = backend.last_call_trace
    assert trace is not None
    assert trace.fallback_to_deterministic is True
    assert trace.fallback_reason == "validation_error"
    # Tokens DID burn — record them
    assert trace.input_tokens == 500
    assert trace.output_tokens == 80


def test_each_call_replaces_trace() -> None:
    """last_call_trace holds at most ONE entry — orchestration drains
    after each call. Two consecutive calls leave only the most-recent
    trace on the backend."""
    p = _Provider(_ok_result({"intent": "DUPLICATE_PO", "confidence": 0.9}))
    backend = _build_backend(p)
    backend.classify_intent(_state(order_id="SO-A"))
    first = backend.last_call_trace
    state2 = _state(order_id="SO-B")
    state2.intent = Intent.DUPLICATE_PO
    p2 = _Provider(_ok_result({"recipe_name": "DuplicatePORecipe.py"}))
    backend2 = _build_backend(p2)
    backend2.propose_recipe(state2)
    second = backend2.last_call_trace

    assert first is not second
    assert first.task == "intent"
    assert second.task == "recipe"


# ---------------------------------------------------------------------------
# Orchestration drain — _drain_llm_trace
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_backend_cache():
    _reset_backend_cache()
    yield
    _reset_backend_cache()


def test_classify_appends_trace_to_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """When classify uses an LLM-backed backend, the per-call trace
    lands on state.llm_call_traces."""
    backend = _build_backend(
        _Provider(_ok_result({"intent": "DUPLICATE_PO", "confidence": 0.95}))
    )
    # Inject directly so the router env doesn't matter
    from orchestration import nodes
    nodes._cached_backends["intent"] = backend  # noqa: SLF001

    state = _state()
    classify(state)
    assert len(state.llm_call_traces) == 1
    assert state.llm_call_traces[0].task == "intent"
    # Drain cleared the backend slot
    assert backend.last_call_trace is None


def test_select_recipe_appends_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _build_backend(
        _Provider(_ok_result({"recipe_name": "DuplicatePORecipe.py"}))
    )
    from orchestration import nodes
    nodes._cached_backends["recipe"] = backend  # noqa: SLF001

    state = _state()
    state.intent = Intent.DUPLICATE_PO
    select_recipe(state)
    assert len(state.llm_call_traces) == 1
    assert state.llm_call_traces[0].task == "recipe"


def test_shadow_audit_appends_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _build_backend(
        _Provider(_ok_result({
            "status": "GREEN", "reasons": ["ok"], "policy_hits": [],
        }))
    )
    from orchestration import nodes
    nodes._cached_backends["shadow"] = backend  # noqa: SLF001

    state = _state()
    state.intent = Intent.DUPLICATE_PO
    shadow_audit(state)
    assert len(state.llm_call_traces) == 1
    assert state.llm_call_traces[0].task == "shadow"


def test_deterministic_backend_no_trace_appended() -> None:
    """DeterministicFallbackBackend has no last_call_trace attribute
    → state.llm_call_traces stays empty."""
    state = _state()
    classify(state)  # default → deterministic
    assert state.llm_call_traces == []


# ---------------------------------------------------------------------------
# Cross-check disagreement signal stamped on the trace
# ---------------------------------------------------------------------------


class _FixedIntentBackend:
    """Test double — returns a fixed intent. Exposes last_call_trace
    so the cross-check stamping path runs."""

    def __init__(self, intent: str):
        self._decision = IntentDecision(intent=intent, confidence=0.95, rationale="x")
        self.last_call_trace: LLMCallTrace | None = None

    def classify_intent(self, state):  # noqa: ARG002
        # Build a representative trace so the cross-check stamp has
        # something to land on. In production this comes from
        # RemoteLLMBackend; in tests we synthesize.
        self.last_call_trace = LLMCallTrace(
            task="intent",
            provider="fake",
            model_id="fake-model",
            request_id="req_fake",
            prompt_hash="0" * 64,
            tool_call_hash="0" * 64,
            input_tokens=10,
            output_tokens=5,
        )
        return self._decision


def test_cross_check_disagreement_stamps_trace() -> None:
    """LLM picks DUPLICATE_PO, deterministic picks
    CONTRACTUAL_CORRECTION → trace records the disagreement."""
    fake = _FixedIntentBackend("DUPLICATE_PO")
    from orchestration import nodes
    nodes._cached_backends["intent"] = fake  # noqa: SLF001

    state = GraphState(
        event=OrderEvent(
            order_id="SO-DISAGREE", line_item=1,
            po_price=90.0, sap_base_price=100.0,
            retailer_id="R-01", line_count=1,
        )
    )
    classify(state)

    assert state.final_status is TerminalStatus.MANUAL_REVIEW_REQUIRED
    assert len(state.llm_call_traces) == 1
    trace = state.llm_call_traces[0]
    assert trace.cross_check_disagreement is True
    assert trace.cross_check_llm_intent == "DUPLICATE_PO"
    assert trace.cross_check_deterministic_intent == "CONTRACTUAL_CORRECTION"


def test_cross_check_agreement_stamps_false_on_trace() -> None:
    fake = _FixedIntentBackend("DUPLICATE_PO")
    from orchestration import nodes
    nodes._cached_backends["intent"] = fake  # noqa: SLF001

    state = _state(order_id="SO-AGREE")  # event_type makes deterministic say DUPLICATE_PO too
    classify(state)

    assert state.final_status is None
    assert len(state.llm_call_traces) == 1
    trace = state.llm_call_traces[0]
    assert trace.cross_check_disagreement is False
    assert trace.cross_check_llm_intent == "DUPLICATE_PO"
    assert trace.cross_check_deterministic_intent == "DUPLICATE_PO"


# ---------------------------------------------------------------------------
# Tracer aggregation
# ---------------------------------------------------------------------------


def test_tracer_aggregates_llm_calls() -> None:
    """Tracer.build_record sums tokens / cost across all llm_calls
    on the state and surfaces flags."""
    state = _state()
    state.llm_call_traces = [
        LLMCallTrace(
            task="intent", provider="anthropic", model_id="claude-sonnet-4-6",
            input_tokens=500, output_tokens=80,
            cache_read_input_tokens=6000, cache_creation_input_tokens=0,
            cost_usd_estimate=0.005,
        ),
        LLMCallTrace(
            task="recipe", provider="anthropic", model_id="claude-sonnet-4-6",
            input_tokens=200, output_tokens=20,
            cache_read_input_tokens=6000, cache_creation_input_tokens=0,
            cost_usd_estimate=0.003,
        ),
    ]

    record = Tracer().build_record(state)
    assert len(record.llm_calls) == 2
    assert record.llm_total_input_tokens == 700
    assert record.llm_total_output_tokens == 100
    assert record.llm_total_cache_read_tokens == 12000
    assert record.llm_total_cost_usd_estimate == pytest.approx(0.008)
    assert record.llm_any_fallback is False
    assert record.llm_cross_check_disagreement is False


def test_tracer_surfaces_any_fallback_flag() -> None:
    state = _state()
    state.llm_call_traces = [
        LLMCallTrace(task="intent", provider="anthropic", model_id="x"),
        LLMCallTrace(
            task="recipe", provider="anthropic", model_id="",
            fallback_to_deterministic=True, fallback_reason="circuit_open",
        ),
    ]
    record = Tracer().build_record(state)
    assert record.llm_any_fallback is True


def test_tracer_surfaces_cross_check_disagreement() -> None:
    state = _state()
    state.llm_call_traces = [
        LLMCallTrace(
            task="intent", provider="anthropic", model_id="x",
            cross_check_disagreement=True,
            cross_check_llm_intent="DUPLICATE_PO",
            cross_check_deterministic_intent="CONTRACTUAL_CORRECTION",
        ),
    ]
    record = Tracer().build_record(state)
    assert record.llm_cross_check_disagreement is True


def test_tracer_no_llm_calls_zero_aggregates() -> None:
    state = _state()
    record = Tracer().build_record(state)
    assert record.llm_calls == []
    assert record.llm_total_input_tokens == 0
    assert record.llm_total_cost_usd_estimate == 0.0
    assert record.llm_any_fallback is False
    assert record.llm_cross_check_disagreement is False


def test_tracerecord_serialises_llm_calls_to_json() -> None:
    """LangFuse forwarding serialises the record; LLMCallTrace must
    survive the round-trip."""
    state = _state()
    state.llm_call_traces = [
        LLMCallTrace(
            task="intent", provider="ollama", model_id="qwen2.5",
            input_tokens=100, output_tokens=20,
        ),
    ]
    record = Tracer().build_record(state)
    payload = record.to_json()
    assert "qwen2.5" in payload
    assert "intent" in payload
    # Parse back and assert structural integrity
    import json
    parsed = json.loads(payload)
    assert parsed["llm_calls"][0]["provider"] == "ollama"
    assert parsed["llm_calls"][0]["task"] == "intent"
