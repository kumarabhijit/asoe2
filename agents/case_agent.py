"""ADR-038 §6.3 — the L3 Case Agent loop.

Boris's `while`-loop, applied. Each iteration:

  1. Build working memory from the L0 skill bundle + per-turn case state.
  2. Call the LLM via the configured provider; receive tool calls or
     a terminal decision.
  3. Execute each tool call through the registry; capture results to
     the case event log.
  4. Deduct cost / iteration / token usage from the case budget.
  5. If the agent declares done / escalates / requests buyer
     clarification, exit.
  6. If budget is exhausted, exit with `budget_exhausted`.

The agent's only non-deterministic role is choosing tools; tools
themselves are L1 deterministic recipes / L2 bounded LLM primitives.
This is the safety property ADR-038 §4.3 names — L3 freedom is
bounded by the L1+L2 surface.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

from agents.budget import CaseBudget
from agents.case_tools import (
    ToolCall,
    ToolContext,
    ToolRegistry,
    ToolResult,
    invoke_tool,
)
from agents.working_memory import WorkingMemoryFrame, build_working_memory
from contracts.models import OrderCase, OrderEvent

logger = logging.getLogger("asoe.agents.case_agent")


# ---------------------------------------------------------------------------
# LLM provider boundary (vendor-bound; tests use a stub)
# ---------------------------------------------------------------------------


class AgentLLMResponse(BaseModel):
    """Per-turn LLM output: zero or more tool calls + an optional
    short reasoning trace (audit-only; never used for control)."""

    model_config = ConfigDict(extra="forbid")

    tool_calls: List[ToolCall] = Field(default_factory=list)
    reasoning: Optional[str] = None
    """Free-form rationale for telemetry. The harness DOES NOT branch
    on this. Control flow is driven by the tool calls alone (Boris:
    'tools are the API contract')."""

    # Token / cost telemetry returned by the provider for budgeting.
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class AgentLLMProvider(Protocol):
    """The single boundary the agent crosses to call an LLM. Production
    implementations: Anthropic / OpenAI / local Ollama. Tests use the
    StubAgentLLMProvider below.
    """

    def call(self, frame: WorkingMemoryFrame) -> AgentLLMResponse:
        """Return a per-turn response. Provider is responsible for
        constraining tool_calls to the names in
        ``frame.tool_descriptors``; the harness double-checks via
        the registry's not-found path (still safe if the provider
        violates the contract)."""
        ...


@dataclass
class StubAgentLLMProvider:
    """Deterministic stub. Tests set ``script`` to a list of
    ``AgentLLMResponse`` objects; the provider returns them in order
    and raises if exhausted (forces tests to be explicit about the
    expected number of turns).
    """

    script: List[AgentLLMResponse] = field(default_factory=list)
    _cursor: int = 0

    def call(self, frame: WorkingMemoryFrame) -> AgentLLMResponse:
        if self._cursor >= len(self.script):
            raise RuntimeError(
                f"StubAgentLLMProvider exhausted: script has "
                f"{len(self.script)} entries, agent asked for index {self._cursor}"
            )
        response = self.script[self._cursor]
        self._cursor += 1
        return response


# ---------------------------------------------------------------------------
# Run result
# ---------------------------------------------------------------------------


AgentRunOutcome = Literal[
    "RESOLVED",
    "ESCALATED",
    "AWAITING_BUYER",
    "AWAITING_ERP",
    "BUDGET_EXHAUSTED",
    "ERROR",
]


class AgentRunResult(BaseModel):
    """Single-event agent run summary."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    outcome: AgentRunOutcome
    halt_reason: Optional[str] = None
    """Free-form when outcome is BUDGET_EXHAUSTED / ERROR; semantic
    reason_code from L0 vocabulary when ESCALATED."""
    iterations: int
    tool_trace: List[Dict[str, Any]] = Field(default_factory=list)
    """Append-only log of (tool_call, result) pairs in invocation order.
    This is the audit-bearing trace; replay reads from here."""
    budget_snapshot: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


_HALT_TOOLS = frozenset({
    "escalate",
    "request_clarification_email",
    "declare_done",
})


def _outcome_for_halt_tool(tool_name: str, data: Dict[str, Any]) -> AgentRunOutcome:
    if tool_name == "escalate":
        return "ESCALATED"
    if tool_name == "request_clarification_email":
        return "AWAITING_BUYER"
    # declare_done — map status to outcome.
    target = str(data.get("case_status_after") or "").upper()
    if target in ("OPEN_AWAITING_ERP", "AWAITING_ERP"):
        return "AWAITING_ERP"
    return "RESOLVED"


def run_case_agent(
    *,
    case: OrderCase,
    skill_name: str,
    current_event: OrderEvent,
    tool_registry: ToolRegistry,
    llm_provider: AgentLLMProvider,
    budget: Optional[CaseBudget] = None,
    last_actions: Optional[List[Dict[str, Any]]] = None,
    tool_extras: Optional[Dict[str, Any]] = None,
) -> AgentRunResult:
    """Boris's `while`-loop made concrete.

    The single non-deterministic step is the LLM call. Tools are
    deterministic (L1 recipes / L2 bounded primitives). All
    invocations + results are logged into ``tool_trace`` so audit
    can replay deterministically.
    """
    budget = budget or CaseBudget.for_case(case)
    actions: List[Dict[str, Any]] = list(last_actions or [])
    tool_trace: List[Dict[str, Any]] = []
    ctx = ToolContext(
        tenant_id=case.tenant_id,
        case_id=case.case_id,
        extras=dict(tool_extras or {}),
    )

    while True:
        # Budget check up-front so we don't issue an LLM call we can't afford.
        exhaustion = budget.is_exhausted()
        if exhaustion is not None:
            return AgentRunResult(
                case_id=case.case_id,
                outcome="BUDGET_EXHAUSTED",
                halt_reason=exhaustion,
                iterations=budget.iterations_used,
                tool_trace=tool_trace,
                budget_snapshot=budget.to_audit_dict(),
            )

        frame = build_working_memory(
            case=case,
            skill_name=skill_name,
            current_event=current_event,
            last_actions=actions,
            tool_registry=tool_registry,
        )

        try:
            response = llm_provider.call(frame)
        except Exception as exc:  # noqa: BLE001 — defensive boundary
            return AgentRunResult(
                case_id=case.case_id,
                outcome="ERROR",
                halt_reason=f"llm_provider_error: {type(exc).__name__}: {exc}",
                iterations=budget.iterations_used,
                tool_trace=tool_trace,
                budget_snapshot=budget.to_audit_dict(),
            )

        # Account for the LLM call.
        budget.deduct(
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
            iterations=1,
        )

        # No tool calls → degenerate response; treat as ERROR.
        if not response.tool_calls:
            return AgentRunResult(
                case_id=case.case_id,
                outcome="ERROR",
                halt_reason="agent returned no tool_calls; refusing to continue",
                iterations=budget.iterations_used,
                tool_trace=tool_trace,
                budget_snapshot=budget.to_audit_dict(),
            )

        # Execute every tool the agent asked for, in order.
        terminal_outcome: Optional[AgentRunOutcome] = None
        terminal_reason: Optional[str] = None
        for call in response.tool_calls:
            result = invoke_tool(tool_registry, ctx, call)
            tool_trace.append({
                "tool_call": call.model_dump(),
                "tool_result": result.model_dump(),
            })
            actions.append({
                "tool": call.tool_name,
                "status": result.status,
                "summary": (result.error or "ok")[:120],
            })
            # Tool's own cost (when reported) — reconcile against estimate.
            if result.cost_usd:
                budget.deduct(cost_usd=result.cost_usd)

            if call.tool_name in _HALT_TOOLS and result.status == "ok":
                terminal_outcome = _outcome_for_halt_tool(
                    call.tool_name, result.data,
                )
                terminal_reason = (
                    str(result.data.get("reason_code") or "")
                    if call.tool_name == "escalate"
                    else None
                )
                break

        if terminal_outcome is not None:
            return AgentRunResult(
                case_id=case.case_id,
                outcome=terminal_outcome,
                halt_reason=terminal_reason,
                iterations=budget.iterations_used,
                tool_trace=tool_trace,
                budget_snapshot=budget.to_audit_dict(),
            )

        # Otherwise loop — next turn the agent observes the latest
        # tool results in working memory and decides the next step.
