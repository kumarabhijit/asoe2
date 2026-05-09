"""ADR-038 Phase H.5 — L4 harness extensions for the Case Agent.

The Case Agent (`agents/case_agent.py`) is a pure inner loop:
budget → working-memory → LLM call → tool dispatch → repeat. This
module is the L4 wrapper that adds the cross-cutting concerns the
agent loop deliberately doesn't own:

  * **Case-aware concurrency lock** — only one agent run per
    `(tenant_id, case_id)` at a time. ADR-038 §6.5 forbids two
    concurrent agent invocations against the same case (working-
    memory inconsistency, double-tool-call risk).
  * **Tool-call interception** — every `(tool_call, tool_result)`
    pair the inner loop emits is also persisted to a per-case
    replay log. The agent does not write to the log; the harness
    does. Audit replay reads from the log alone.
  * **Tier graduation** — Automated Orders open at T2 lazily;
    when the agent's first non-clean event lands, the case
    graduates to T3 (compacted view). Forward-only per ADR-038
    §7.1.
  * **Compaction trigger** — after every harness step, evaluate
    `agents.compaction.apply_compaction_if_needed` so the case's
    working-memory summary stays bounded.
  * **L2 LLM Shadow observe-only invocation** — for ADR-039 X.1,
    invoke the L2 Shadow on triggered events and stamp the
    verdict onto the persisted record. Phase X.1 does **not**
    let it move the deterministic verdict; it is recorded for
    telemetry only.

The harness is the single L4 entry point; callers go through
`run_agent_step` and never touch the inner loop directly. The
deterministic graph stays unchanged — the harness is invoked
from a routing predicate (`should_route_to_case_agent`) that
the live API surface enables once Compliance ratifies (still
pending; today it returns False unconditionally).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agents.budget import CaseBudget
from agents.case_agent import AgentRunResult, run_case_agent
from agents.case_tools import ToolRegistry
from agents.compaction import CompactionResult, apply_compaction_if_needed
from compliance.shadow_llm import (
    ShadowLLM,
    ShadowLLMOutcome,
    ShadowLLMRequest,
)
from contracts.models import (
    ComplianceDecision,
    OrderCase,
    OrderEvent,
    ShadowStatus,
)

logger = logging.getLogger("asoe.agents.harness")


# ---------------------------------------------------------------------------
# Concurrency lock — per (tenant_id, case_id)
# ---------------------------------------------------------------------------


class CaseLockManager:
    """Per-case mutex; ADR-038 §6.5 invariant.

    Two agent invocations on the same case must never run
    concurrently — working-memory loaders read the case's
    persisted state and a parallel run would step on its own
    persistence. Locking is per-case so unrelated cases proceed
    in parallel.

    The implementation is intentionally tiny: a dict-of-locks
    behind a guard lock. The dict grows; we don't GC entries
    (the keys are case-id strings and bounded per-tenant by case
    volume). When the SQL backend lands the mechanism becomes
    `SELECT FOR UPDATE NOWAIT` on the case row; in-memory mode
    is faithful to that semantic.
    """

    def __init__(self) -> None:
        self._guard = threading.RLock()
        self._locks: Dict[tuple[str, str], threading.RLock] = {}

    def acquire(self, tenant_id: str, case_id: str) -> threading.RLock:
        with self._guard:
            key = (tenant_id, case_id)
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
        lock.acquire()
        return lock

    def try_acquire(
        self, tenant_id: str, case_id: str,
    ) -> Optional[threading.RLock]:
        """Non-blocking acquire. Returns None when the case is
        already held — caller should re-queue the event."""
        with self._guard:
            key = (tenant_id, case_id)
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
        if lock.acquire(blocking=False):
            return lock
        return None


_default_lock_manager = CaseLockManager()


# ---------------------------------------------------------------------------
# Tool-call interceptor / replay log
# ---------------------------------------------------------------------------


@dataclass
class ToolCallReplayEntry:
    """One persisted tool-call record. Append-only.

    Mirrors `AgentRunResult.tool_trace` entries but with a stable
    case-scoped event id and the timestamp the L4 harness saw the
    call (not when the agent emitted it). The latter discrepancy
    is intentional: replay must run against the same wall-clock
    sequence the harness observed.
    """

    event_id: str
    case_id: str
    tenant_id: str
    occurred_at: str
    tool_name: str
    tool_call: Dict[str, Any]
    tool_result: Dict[str, Any]
    outcome: str  # AgentRunResult.outcome at end of step (may be in-flight)


class ToolCallReplayLog:
    """In-memory replay store. The DB-backed equivalent
    (`case_events` table) ships in a future phase; the in-memory
    implementation here exercises the API surface and is reset by
    tests."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: List[ToolCallReplayEntry] = []

    def record(self, entry: ToolCallReplayEntry) -> None:
        with self._lock:
            self._entries.append(entry)

    def list_for_case(self, case_id: str) -> List[ToolCallReplayEntry]:
        with self._lock:
            return [e for e in self._entries if e.case_id == case_id]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_default_replay_log = ToolCallReplayLog()


# ---------------------------------------------------------------------------
# Tier graduation
# ---------------------------------------------------------------------------


def graduate_tier_if_needed(
    *,
    case: OrderCase,
    is_clean_event: bool,
) -> Optional[OrderCase]:
    """ADR-038 §7.1 forward-only tier graduation.

    Automated Orders open at T2 (stateful) lazily on the first
    non-clean event. T2 → T3 (compacted) graduation fires the
    first time an *agent* runs against the case (the moment we
    accept the case will accumulate non-trivial state).

    Returns the updated `OrderCase` when graduation fired; None
    when no change. Persistence is the caller's responsibility
    (the harness writes back via `case_store.update`).
    """
    # Manual orders open at T2 already; they only graduate on the
    # first agent run touching them.
    if is_clean_event:
        return None
    # Forward-only: T1 → T2, T2 → T3. Skip when already at the
    # higher tier.
    next_tier: Optional[int] = None
    if case.tier == 1:
        next_tier = 2
    elif case.tier == 2:
        next_tier = 3
    if next_tier is None:
        return None
    return case.model_copy(update={"tier": next_tier})


# ---------------------------------------------------------------------------
# Step result + the harness entry point
# ---------------------------------------------------------------------------


@dataclass
class HarnessStepResult:
    """Everything the harness produced for one event.

    The agent's `AgentRunResult` is the inner-loop output; this
    dataclass wraps it with the cross-cutting effects the harness
    contributed: tier graduation, compaction, L2 Shadow verdict,
    replay-log entries.
    """

    agent_result: AgentRunResult
    case: OrderCase  # post-step snapshot (may differ from input via graduation)
    replay_entries: List[ToolCallReplayEntry] = field(default_factory=list)
    compaction: Optional[CompactionResult] = None
    shadow_llm: Optional[ShadowLLMOutcome] = None
    skipped_reason: Optional[str] = None
    """Set when the harness short-circuited (e.g., concurrency
    lock contention) and the inner loop did not run."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_agent_step(
    *,
    case: OrderCase,
    skill_name: str,
    current_event: OrderEvent,
    tool_registry: ToolRegistry,
    llm_provider,
    is_clean_event: bool = False,
    deterministic_decision: Optional[ComplianceDecision] = None,
    financial_impact_usd: float = 0.0,
    shadow_llm: Optional[ShadowLLM] = None,
    budget: Optional[CaseBudget] = None,
    last_actions: Optional[List[Dict[str, Any]]] = None,
    tool_extras: Optional[Dict[str, Any]] = None,
    lock_manager: CaseLockManager = _default_lock_manager,
    replay_log: ToolCallReplayLog = _default_replay_log,
) -> HarnessStepResult:
    """Single-event L4 harness step.

    This is the function the live API surface (or the future
    routing-predicate'd graph node) calls per inbound event. It
    composes:

      1. Concurrency lock acquisition (best-effort; bail if held).
      2. Tier graduation, persisted to the case row.
      3. The Case Agent inner loop (`run_case_agent`).
      4. Tool-call interception → replay log.
      5. Compaction trigger evaluation against the new replay log.
      6. L2 LLM Shadow observe-only invocation (when configured).

    The harness does **not** invoke the deterministic graph — it
    is downstream of it. The caller hands in
    `deterministic_decision` from the existing
    `compliance/shadow.py::ComplianceShadow` so L2 Shadow gating
    can use it without re-running the L1 path.
    """
    lock = lock_manager.try_acquire(case.tenant_id, case.case_id)
    if lock is None:
        return HarnessStepResult(
            agent_result=AgentRunResult(
                case_id=case.case_id,
                outcome="ERROR",
                halt_reason="case_locked: concurrent agent run in progress",
                iterations=0,
            ),
            case=case,
            skipped_reason="case_locked",
        )

    try:
        # --- 1. Tier graduation -----------------------------------
        from api.store import case_store

        graduated = graduate_tier_if_needed(
            case=case, is_clean_event=is_clean_event,
        )
        if graduated is not None:
            case_store.update(case.case_id, tier=graduated.tier)
            case = graduated

        # --- 2. Inner agent loop ----------------------------------
        agent_result = run_case_agent(
            case=case,
            skill_name=skill_name,
            current_event=current_event,
            tool_registry=tool_registry,
            llm_provider=llm_provider,
            budget=budget,
            last_actions=last_actions,
            tool_extras=tool_extras,
        )

        # --- 3. Tool-call interception → replay log ---------------
        replay_entries = _persist_tool_trace(
            agent_result=agent_result,
            case=case,
            replay_log=replay_log,
        )

        # --- 4. Compaction --------------------------------------
        compaction_inputs = [
            {
                "event_type": entry.tool_name,
                "timestamp": entry.occurred_at,
                "outcome": entry.tool_result.get("status") or "",
            }
            for entry in replay_log.list_for_case(case.case_id)
        ]
        compaction = apply_compaction_if_needed(
            case=case, events=compaction_inputs,
        )

        # --- 5. L2 LLM Shadow observe-only ----------------------
        shadow_outcome: Optional[ShadowLLMOutcome] = None
        if shadow_llm is not None and deterministic_decision is not None:
            request = ShadowLLMRequest(
                intent=str(getattr(current_event, "intent", "") or ""),
                recipe_name=skill_name,
                recipe_params={"order_id": current_event.order_id},
                proposed_action=agent_result.outcome,
                deterministic_status=deterministic_decision.status.value,
                deterministic_reasons=tuple(deterministic_decision.reasons or []),
                deterministic_policy_hits=tuple(
                    deterministic_decision.policy_hits or [],
                ),
                case_context_summary=case.working_memory_summary,
                customer_profile={"customer_id": case.customer_id or ""},
            )
            shadow_outcome = shadow_llm.evaluate(
                tenant_id=case.tenant_id,
                request=request,
                deterministic=deterministic_decision,
                financial_impact_usd=financial_impact_usd,
            )

        return HarnessStepResult(
            agent_result=agent_result,
            case=case_store.get(case.case_id) or case,
            replay_entries=replay_entries,
            compaction=compaction,
            shadow_llm=shadow_outcome,
        )
    finally:
        lock.release()


def _persist_tool_trace(
    *,
    agent_result: AgentRunResult,
    case: OrderCase,
    replay_log: ToolCallReplayLog,
) -> List[ToolCallReplayEntry]:
    """Translate the inner loop's `tool_trace` into the harness's
    typed replay entries and append them. The inner loop emits
    plain dicts for size; the harness lifts them into the audit-
    bearing shape."""
    entries: List[ToolCallReplayEntry] = []
    now = _now_iso()
    for idx, step in enumerate(agent_result.tool_trace):
        call = step.get("tool_call") or {}
        result = step.get("tool_result") or {}
        entry = ToolCallReplayEntry(
            event_id=f"{case.case_id}:{agent_result.iterations}:{idx}",
            case_id=case.case_id,
            tenant_id=case.tenant_id,
            occurred_at=now,
            tool_name=str(call.get("tool_name") or ""),
            tool_call=dict(call),
            tool_result=dict(result),
            outcome=agent_result.outcome,
        )
        entries.append(entry)
        replay_log.record(entry)
    return entries


# ---------------------------------------------------------------------------
# Routing predicate
# ---------------------------------------------------------------------------


# Event types the harness is wired to handle. Restricted to
# Manual-Order events for the X.1 / H.5 cutover; Automated-Order
# events stay on the deterministic graph until later phases.
ROUTABLE_EVENT_TYPES = frozenset({"EMAIL_ORDER_ENTRY_REQUEST"})


def should_route_to_case_agent(
    event: OrderEvent,
    *,
    enabled: bool = False,
) -> bool:
    """Routing predicate the live API surface consults.

    Returns True iff the event is in the routable set AND the
    `enabled` config flag is on. The flag defaults to False until
    Compliance ratifies the rollout (ADR-038 §H.5 + ADR-039 §6.1
    workshop gates). Code-wise the wiring is in place; the
    config flip is a one-line change in the live deployment.

    Callers pattern (sketch — actual wire-up is the post-
    ratification follow-up):

        if should_route_to_case_agent(event,
            enabled=settings.case_agent_enabled,
        ):
            harness.run_agent_step(...)
        else:
            run_graph(state)
    """
    if not enabled:
        return False
    return event.event_type in ROUTABLE_EVENT_TYPES
