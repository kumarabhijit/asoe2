"""ADR-038 Phase H.5 — Case Agent + harness tests.

Locks the contracts:

  * CaseBudget enforces per-tier limits per ADR-038 §8.1.
  * ToolRegistry dispatches tools; missing tool returns 'not_found',
    raising tool returns 'error' (no exception escapes).
  * WorkingMemoryFrame composes per ADR-038 §5.3 cache-discipline order.
  * run_case_agent loops until terminal tool call OR budget exhaustion.
  * Tool trace is the audit-bearing artefact (every call + result logged).
"""

from __future__ import annotations

import pytest

from agents.budget import CaseBudget
from agents.case_agent import (
    AgentLLMResponse,
    AgentRunResult,
    StubAgentLLMProvider,
    run_case_agent,
)
from agents.case_tools import (
    ToolCall,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_default_registry,
    invoke_tool,
)
from agents.primitives.extract_attachment import (
    ExtractedField,
    StubMultimodalProvider,
    extraction_cache,
    fingerprint_for_template,
)
from agents.working_memory import (
    build_working_memory,
    cache_prefix_segments,
    per_turn_segments,
)
from api.store import case_store
from contracts.models import OrderCase, OrderEvent


@pytest.fixture(autouse=True)
def _reset_stores():
    case_store.clear()
    extraction_cache.clear()
    yield
    case_store.clear()
    extraction_cache.clear()


@pytest.fixture
def case() -> OrderCase:
    """A T2 case ready for the agent to run on."""
    case, _ = case_store.lookup_or_create(
        tenant_id="t1",
        source="manual_order",
        source_channel="email",
        customer_id="acct-southeast",
        customer_po_number="EML-PO-2026-0042",
    )
    return case


@pytest.fixture
def event() -> OrderEvent:
    return OrderEvent(
        order_id="EML-PO-2026-0042",
        po_price=18_400.0,
        sap_base_price=18_400.0,
        event_type="EMAIL_ORDER_ENTRY_REQUEST",
        retailer_id="acct-southeast",
        line_count=4,
        metadata={"composite_confidence": 0.88},
    )


@pytest.fixture
def registry() -> ToolRegistry:
    return build_default_registry()


# ---------------------------------------------------------------------------
# CaseBudget
# ---------------------------------------------------------------------------


class TestCaseBudget:
    def test_tier_2_limits_match_adr(self):
        b = CaseBudget.for_tier(2)
        assert b.max_input_tokens == 16_000
        assert b.max_output_tokens == 4_000
        assert b.max_iterations == 6
        assert b.max_wall_clock_ms == 8_000
        assert b.max_cost_usd == 0.05

    def test_tier_1_limits_match_adr(self):
        b = CaseBudget.for_tier(1)
        assert b.max_input_tokens == 4_000
        assert b.max_iterations == 1

    def test_unknown_tier_raises(self):
        with pytest.raises(ValueError):
            CaseBudget.for_tier(99)

    def test_deduct_accumulates(self):
        b = CaseBudget.for_tier(2)
        b.deduct(input_tokens=1000, output_tokens=200, cost_usd=0.005, iterations=1)
        b.deduct(input_tokens=500, cost_usd=0.002)
        assert b.input_tokens_used == 1500
        assert b.output_tokens_used == 200
        assert b.cost_usd_used == pytest.approx(0.007)
        assert b.iterations_used == 1

    def test_exhaustion_on_iterations(self):
        b = CaseBudget.for_tier(1)
        assert b.is_exhausted() is None
        b.deduct(iterations=1)
        reason = b.is_exhausted()
        assert reason is not None and "iterations" in reason

    def test_exhaustion_on_cost(self):
        b = CaseBudget.for_tier(2)
        b.deduct(cost_usd=0.06)  # exceeds 0.05 cap
        assert "cost_usd" in (b.is_exhausted() or "")

    def test_for_case_uses_case_tier(self):
        case = OrderCase(
            tenant_id="t1",
            source="manual_order",
            source_channel="email",
            tier=3,
        )
        b = CaseBudget.for_case(case)
        assert b.tier == 3


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


class TestToolRegistry:
    def test_default_registry_has_minimum_set(self, registry: ToolRegistry):
        # Phase H.5 ships at least 9 tools per ADR-038 §6.4 (subset).
        names = registry.names()
        for required in (
            "read_case_summary", "extract_attachment", "check_credit",
            "check_duplicate_po", "check_moq", "write_case_note",
            "request_clarification_email", "escalate", "declare_done",
        ):
            assert required in names

    def test_invoke_unknown_tool_returns_not_found(self):
        registry = ToolRegistry()
        ctx = ToolContext(tenant_id="t1", case_id="c1")
        result = invoke_tool(
            registry, ctx, ToolCall(tool_name="ghost"),
        )
        assert result.status == "not_found"
        assert "ghost" in result.error

    def test_invoke_handler_exception_returns_error_not_raise(self):
        def boom(ctx, args):
            raise RuntimeError("kaboom")
        registry = ToolRegistry()
        registry.register(ToolSpec(name="boom", description="x", handler=boom))
        ctx = ToolContext(tenant_id="t1", case_id="c1")
        result = invoke_tool(
            registry, ctx, ToolCall(tool_name="boom"),
        )
        # No exception escaped; the loop continues.
        assert result.status == "error"
        assert "kaboom" in result.error


# ---------------------------------------------------------------------------
# Built-in tools
# ---------------------------------------------------------------------------


class TestBuiltInTools:
    def test_read_case_summary_returns_case_state(self, case, registry):
        ctx = ToolContext(tenant_id="t1", case_id=case.case_id)
        result = invoke_tool(
            registry, ctx, ToolCall(tool_name="read_case_summary"),
        )
        assert result.status == "ok"
        assert result.data["case_id"] == case.case_id
        assert result.data["source"] == "manual_order"

    def test_read_case_summary_unknown_case(self, registry):
        ctx = ToolContext(tenant_id="t1", case_id="ghost")
        result = invoke_tool(
            registry, ctx, ToolCall(tool_name="read_case_summary"),
        )
        assert result.status == "not_found"

    def test_write_case_note_persists(self, case, registry):
        ctx = ToolContext(tenant_id="t1", case_id=case.case_id)
        result = invoke_tool(
            registry, ctx,
            ToolCall(tool_name="write_case_note", arguments={"note": "looks ok"}),
        )
        assert result.status == "ok"
        # Re-read summary; note appears in the working_memory_summary.
        retrieved = case_store.get(case.case_id)
        assert "looks ok" in (retrieved.working_memory_summary or "")

    def test_check_moq_runs_recipe(self, case, registry):
        ctx = ToolContext(tenant_id="t1", case_id=case.case_id)
        result = invoke_tool(
            registry, ctx,
            ToolCall(tool_name="check_moq", arguments={
                "sku": "SKU-1", "ordered_qty": 100, "moq_qty": 200,
                "unit_cost": 5.0, "uom": "CS",
            }),
        )
        assert result.status == "ok"
        assert "status" in result.data  # MOQ recipe output shape

    def test_extract_attachment_uses_provider_from_extras(self, case, registry):
        provider = StubMultimodalProvider()
        provider.register_fixture(
            fingerprint_for_template("po.pdf"),
            [ExtractedField(name="po_number", value="X-1", confidence=0.99)],
        )
        ctx = ToolContext(
            tenant_id="t1",
            case_id=case.case_id,
            extras={"multimodal_provider": provider},
        )
        result = invoke_tool(
            registry, ctx,
            ToolCall(tool_name="extract_attachment", arguments={
                "attachment": {
                    "name": "po.pdf",
                    "mime_type": "application/pdf",
                    "bytes": 1234,
                },
            }),
        )
        assert result.status == "ok"
        fields = result.data["fields"]
        assert any(f["name"] == "po_number" for f in fields)


# ---------------------------------------------------------------------------
# Working memory builder
# ---------------------------------------------------------------------------


class TestWorkingMemory:
    def test_frame_carries_skill_md_when_bundle_exists(self, case, event, registry):
        # email-order-entry bundle doesn't exist on this branch (per
        # rollout plan §3 Option C), so use an existing bundle name.
        frame = build_working_memory(
            case=case,
            skill_name="duplicate-po",
            current_event=event,
            tool_registry=registry,
        )
        assert frame.skill_md  # non-empty
        # Anchor examples list — bundle ships empty anchors per §5.5
        assert frame.anchor_examples == []
        # Tool descriptors mirror the registry.
        assert {d["name"] for d in frame.tool_descriptors} >= {
            "read_case_summary", "escalate", "declare_done",
        }

    def test_unknown_skill_returns_empty_skill_md(self, case, event, registry):
        frame = build_working_memory(
            case=case,
            skill_name="never-exists",
            current_event=event,
            tool_registry=registry,
        )
        assert frame.skill_md == ""
        assert frame.anchor_examples == []

    def test_cache_prefix_order_is_stable(self, case, event, registry):
        """ADR-038 §5.3 — cached prefix is system → SKILL.md → anchors
        → manifest → tools. Order regression must be loud."""
        frame = build_working_memory(
            case=case,
            skill_name="duplicate-po",
            current_event=event,
            tool_registry=registry,
        )
        prefix = cache_prefix_segments(frame)
        # First segment is the system prompt.
        assert "Case Agent" in prefix[0]
        # Second segment is SKILL.md (carries the skill's name).
        assert "duplicate" in prefix[1].lower()

    def test_per_turn_segments_serialise_case_summary(self, case, event, registry):
        frame = build_working_memory(
            case=case,
            skill_name="duplicate-po",
            current_event=event,
            tool_registry=registry,
        )
        segs = per_turn_segments(frame)
        joined = "\n".join(segs)
        assert case.case_id in joined
        # The current-event payload's order_id must round-trip through
        # the JSON dump.
        assert event.order_id in joined


# ---------------------------------------------------------------------------
# Agent loop (end-to-end with stub provider)
# ---------------------------------------------------------------------------


class TestAgentLoop:
    def test_single_iteration_declare_done(self, case, event, registry):
        provider = StubAgentLLMProvider(script=[
            AgentLLMResponse(
                tool_calls=[
                    ToolCall(tool_name="declare_done", arguments={"status": "RESOLVED"}),
                ],
                input_tokens=2000, output_tokens=100, cost_usd=0.005,
            ),
        ])
        result = run_case_agent(
            case=case, skill_name="duplicate-po",
            current_event=event, tool_registry=registry,
            llm_provider=provider,
        )
        assert isinstance(result, AgentRunResult)
        assert result.outcome == "RESOLVED"
        assert result.iterations == 1
        # Tool trace logs the declare_done call.
        assert len(result.tool_trace) == 1
        assert result.tool_trace[0]["tool_call"]["tool_name"] == "declare_done"

    def test_multi_iteration_then_escalate(self, case, event, registry):
        provider = StubAgentLLMProvider(script=[
            AgentLLMResponse(
                tool_calls=[ToolCall(tool_name="read_case_summary")],
                input_tokens=2000, output_tokens=200, cost_usd=0.005,
            ),
            AgentLLMResponse(
                tool_calls=[ToolCall(
                    tool_name="check_moq", arguments={
                        "sku": "SKU-1", "ordered_qty": 100,
                        "moq_qty": 200, "unit_cost": 5.0, "uom": "CS",
                    },
                )],
                input_tokens=2500, output_tokens=200, cost_usd=0.006,
            ),
            AgentLLMResponse(
                tool_calls=[ToolCall(
                    tool_name="escalate", arguments={
                        "reason_code": "moq_severe_shortfall",
                        "target_role": "manager",
                    },
                )],
                input_tokens=2500, output_tokens=100, cost_usd=0.005,
            ),
        ])
        result = run_case_agent(
            case=case, skill_name="moq-round-up",
            current_event=event, tool_registry=registry,
            llm_provider=provider,
        )
        assert result.outcome == "ESCALATED"
        assert result.halt_reason == "moq_severe_shortfall"
        assert result.iterations == 3
        assert len(result.tool_trace) == 3

    def test_request_clarification_email_outcome(self, case, event, registry):
        provider = StubAgentLLMProvider(script=[
            AgentLLMResponse(
                tool_calls=[ToolCall(
                    tool_name="request_clarification_email",
                    arguments={
                        "template": "ship_to_disambiguation.template.md",
                        "fields": {"options": ["Atlanta-North", "Atlanta-South"]},
                    },
                )],
                input_tokens=2000, output_tokens=200, cost_usd=0.005,
            ),
        ])
        result = run_case_agent(
            case=case, skill_name="duplicate-po",
            current_event=event, tool_registry=registry,
            llm_provider=provider,
        )
        assert result.outcome == "AWAITING_BUYER"
        assert result.iterations == 1

    def test_budget_exhaustion_returns_typed_outcome(self, case, event, registry):
        # T1 budget allows 1 iteration; ask the agent to run two.
        provider = StubAgentLLMProvider(script=[
            AgentLLMResponse(
                tool_calls=[ToolCall(tool_name="read_case_summary")],
                input_tokens=100, output_tokens=50, cost_usd=0.0005,
            ),
            AgentLLMResponse(
                tool_calls=[ToolCall(tool_name="declare_done")],
                input_tokens=100, output_tokens=50, cost_usd=0.0005,
            ),
        ])
        budget = CaseBudget.for_tier(1)  # 1 iteration max
        result = run_case_agent(
            case=case, skill_name="duplicate-po",
            current_event=event, tool_registry=registry,
            llm_provider=provider, budget=budget,
        )
        assert result.outcome == "BUDGET_EXHAUSTED"
        assert "iterations" in (result.halt_reason or "")
        # First iteration ran; second was preempted before the LLM call.
        assert result.iterations == 1

    def test_llm_provider_error_returns_typed_outcome(self, case, event, registry):
        class BoomProvider:
            def call(self, frame):
                raise RuntimeError("provider down")
        result = run_case_agent(
            case=case, skill_name="duplicate-po",
            current_event=event, tool_registry=registry,
            llm_provider=BoomProvider(),
        )
        assert result.outcome == "ERROR"
        assert "provider down" in (result.halt_reason or "")

    def test_no_tool_calls_returns_error_not_loop(self, case, event, registry):
        # Agent that returns no tool_calls is degenerate — must be
        # caught (Boris invariant: tools are control flow).
        provider = StubAgentLLMProvider(script=[
            AgentLLMResponse(tool_calls=[], reasoning="thinking..."),
        ])
        result = run_case_agent(
            case=case, skill_name="duplicate-po",
            current_event=event, tool_registry=registry,
            llm_provider=provider,
        )
        assert result.outcome == "ERROR"
        assert "no tool_calls" in (result.halt_reason or "")

    def test_tool_trace_replayability(self, case, event, registry):
        """The tool trace must be deterministically replayable: same
        case state + same provider script + same registry → same trace."""
        provider_a = StubAgentLLMProvider(script=[
            AgentLLMResponse(
                tool_calls=[ToolCall(tool_name="declare_done")],
                input_tokens=100, output_tokens=50,
            ),
        ])
        provider_b = StubAgentLLMProvider(script=[
            AgentLLMResponse(
                tool_calls=[ToolCall(tool_name="declare_done")],
                input_tokens=100, output_tokens=50,
            ),
        ])
        result_a = run_case_agent(
            case=case, skill_name="duplicate-po",
            current_event=event, tool_registry=registry,
            llm_provider=provider_a,
        )
        # Reset case-store side effects of A so B sees the same starting
        # state. The agent loop is pure given (case, event, provider).
        case_store.clear()
        case_b, _ = case_store.lookup_or_create(
            tenant_id=case.tenant_id, source=case.source,
            source_channel=case.source_channel,
            customer_po_number=case.customer_po_number,
        )
        result_b = run_case_agent(
            case=case_b, skill_name="duplicate-po",
            current_event=event, tool_registry=registry,
            llm_provider=provider_b,
        )
        # Same tool names + statuses across runs (case_id varies because
        # IDs are generated; the trace structure is stable).
        a_tools = [e["tool_call"]["tool_name"] for e in result_a.tool_trace]
        b_tools = [e["tool_call"]["tool_name"] for e in result_b.tool_trace]
        assert a_tools == b_tools
