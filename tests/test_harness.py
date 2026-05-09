"""ADR-038 Phase H.5 — `agents/harness.py` L4 wrapper tests.

The harness composes:
  * concurrency lock per (tenant, case)
  * tier graduation (forward-only)
  * inner-loop invocation
  * tool-call interception → replay log
  * compaction trigger evaluation
  * ADR-039 X.1 observe-only L2 LLM Shadow invocation

Tests cover each composition seam in isolation plus a happy-path
end-to-end run that touches every step. The inner agent loop is
already covered by `tests/test_case_agent.py`; tests here use the
stub provider.
"""

from __future__ import annotations

import threading
import time

import pytest

from agents.case_agent import AgentLLMResponse, StubAgentLLMProvider
from agents.case_tools import ToolCall, ToolRegistry, build_default_registry
from agents.harness import (
    ROUTABLE_EVENT_TYPES,
    CaseLockManager,
    HarnessStepResult,
    ToolCallReplayEntry,
    ToolCallReplayLog,
    graduate_tier_if_needed,
    run_agent_step,
    should_route_to_case_agent,
)
from api.store import case_store, exception_store
from compliance.shadow_llm import (
    ShadowLLM,
    StubLLMShadowProvider,
    load_bundle,
    shadow_llm_cache,
    shadow_llm_metrics,
)
from contracts.models import (
    ComplianceDecision,
    OrderCase,
    OrderEvent,
    ShadowStatus,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_state():
    case_store.clear()
    exception_store.clear()
    shadow_llm_cache.clear()
    shadow_llm_metrics.reset()
    yield
    case_store.clear()
    exception_store.clear()
    shadow_llm_cache.clear()
    shadow_llm_metrics.reset()


@pytest.fixture
def case() -> OrderCase:
    case, _ = case_store.lookup_or_create(
        tenant_id="t1",
        source="manual_order",
        source_channel="email",
        customer_po_number="PO-1",
        customer_id="acc-001",
    )
    return case


@pytest.fixture
def event() -> OrderEvent:
    return OrderEvent(
        order_id="PO-1",
        po_price=120.0,
        sap_base_price=120.0,
        event_type="EMAIL_ORDER_ENTRY_REQUEST",
        retailer_id="acc-001",
    )


@pytest.fixture
def registry() -> ToolRegistry:
    return build_default_registry()


@pytest.fixture
def declare_done_provider():
    return StubAgentLLMProvider(script=[
        AgentLLMResponse(
            tool_calls=[
                ToolCall(tool_name="declare_done", arguments={"status": "RESOLVED"}),
            ],
            input_tokens=2000, output_tokens=100, cost_usd=0.005,
        ),
    ])


# ---------------------------------------------------------------------------
# Concurrency lock
# ---------------------------------------------------------------------------

class TestCaseLockManager:
    def test_first_acquire_succeeds(self):
        mgr = CaseLockManager()
        lock = mgr.try_acquire("t1", "c1")
        assert lock is not None
        lock.release()

    def test_second_concurrent_acquire_returns_none(self):
        mgr = CaseLockManager()
        first = mgr.try_acquire("t1", "c1")
        assert first is not None
        try:
            # Spawn a thread to attempt the same key — RLock only
            # allows the owning thread to re-enter, so a second
            # thread should miss.
            holder: dict[str, object] = {}

            def worker():
                holder["second"] = mgr.try_acquire("t1", "c1")

            t = threading.Thread(target=worker)
            t.start()
            t.join(timeout=2)
            assert holder["second"] is None
        finally:
            first.release()

    def test_different_cases_dont_block(self):
        mgr = CaseLockManager()
        a = mgr.try_acquire("t1", "case-a")
        b = mgr.try_acquire("t1", "case-b")
        assert a is not None and b is not None
        a.release()
        b.release()

    def test_tenant_partitioned(self):
        mgr = CaseLockManager()
        a = mgr.try_acquire("tenant-a", "c1")
        b = mgr.try_acquire("tenant-b", "c1")
        assert a is not None and b is not None
        a.release()
        b.release()


# ---------------------------------------------------------------------------
# Tier graduation
# ---------------------------------------------------------------------------

class TestTierGraduation:
    def test_clean_event_no_change(self, case):
        graduated = graduate_tier_if_needed(case=case, is_clean_event=True)
        assert graduated is None

    def test_t2_graduates_to_t3(self, case):
        # Default case is T2 (lookup_or_create with no override).
        assert case.tier == 2
        graduated = graduate_tier_if_needed(case=case, is_clean_event=False)
        assert graduated is not None
        assert graduated.tier == 3

    def test_t3_does_not_regress_or_advance(self, case):
        case = case.model_copy(update={"tier": 3})
        graduated = graduate_tier_if_needed(case=case, is_clean_event=False)
        assert graduated is None  # already top tier

    def test_t1_graduates_to_t2(self, case):
        case = case.model_copy(update={"tier": 1})
        graduated = graduate_tier_if_needed(case=case, is_clean_event=False)
        assert graduated is not None
        assert graduated.tier == 2


# ---------------------------------------------------------------------------
# Tool-call replay log
# ---------------------------------------------------------------------------

class TestReplayLog:
    def test_records_and_lists_per_case(self):
        log = ToolCallReplayLog()
        log.record(ToolCallReplayEntry(
            event_id="c1:0:0", case_id="c1", tenant_id="t1",
            occurred_at="2026-01-01T00:00:00Z", tool_name="declare_done",
            tool_call={}, tool_result={}, outcome="RESOLVED",
        ))
        log.record(ToolCallReplayEntry(
            event_id="c2:0:0", case_id="c2", tenant_id="t1",
            occurred_at="2026-01-01T00:00:01Z", tool_name="escalate",
            tool_call={}, tool_result={}, outcome="ESCALATED",
        ))
        assert len(log.list_for_case("c1")) == 1
        assert len(log.list_for_case("c2")) == 1
        assert log.list_for_case("nonexistent") == []


# ---------------------------------------------------------------------------
# End-to-end harness step
# ---------------------------------------------------------------------------

class TestRunAgentStep:
    def test_happy_path(self, case, event, registry, declare_done_provider):
        result = run_agent_step(
            case=case, skill_name="email-order-entry",
            current_event=event, tool_registry=registry,
            llm_provider=declare_done_provider,
        )
        assert isinstance(result, HarnessStepResult)
        assert result.agent_result.outcome == "RESOLVED"
        assert len(result.replay_entries) == 1
        # Tier graduated 2 → 3 because event is non-clean.
        assert result.case.tier == 3
        # Persistence happened.
        persisted = case_store.get(case.case_id)
        assert persisted.tier == 3

    def test_concurrency_lock_short_circuits(
        self, case, event, registry, declare_done_provider,
    ):
        mgr = CaseLockManager()
        # Pre-acquire from "another thread" by holding the lock here.
        holder: dict[str, object] = {}

        def hold():
            holder["lock"] = mgr.acquire(case.tenant_id, case.case_id)
            time.sleep(0.5)
            holder["lock"].release()  # type: ignore[union-attr]

        t = threading.Thread(target=hold)
        t.start()
        time.sleep(0.05)  # let the thread acquire
        try:
            result = run_agent_step(
                case=case, skill_name="email-order-entry",
                current_event=event, tool_registry=registry,
                llm_provider=declare_done_provider,
                lock_manager=mgr,
            )
            assert result.skipped_reason == "case_locked"
            assert result.agent_result.outcome == "ERROR"
            assert "case_locked" in (result.agent_result.halt_reason or "")
        finally:
            t.join(timeout=2)

    def test_tool_trace_persisted_to_replay_log(
        self, case, event, registry, declare_done_provider,
    ):
        log = ToolCallReplayLog()
        run_agent_step(
            case=case, skill_name="email-order-entry",
            current_event=event, tool_registry=registry,
            llm_provider=declare_done_provider,
            replay_log=log,
        )
        entries = log.list_for_case(case.case_id)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.tool_name == "declare_done"
        assert entry.tenant_id == case.tenant_id
        assert entry.outcome == "RESOLVED"

    def test_l2_shadow_invoked_when_provided(
        self, case, event, registry, declare_done_provider,
    ):
        bundle = load_bundle()
        shadow = ShadowLLM(provider=StubLLMShadowProvider(), bundle=bundle)
        decision = ComplianceDecision(status=ShadowStatus.GREEN)
        result = run_agent_step(
            case=case, skill_name="email-order-entry",
            current_event=event, tool_registry=registry,
            llm_provider=declare_done_provider,
            deterministic_decision=decision,
            financial_impact_usd=1_000.0,
            shadow_llm=shadow,
        )
        assert result.shadow_llm is not None
        # X.1 default stub returns ABSTAIN on default GREEN inputs.
        assert result.shadow_llm.verdict is not None
        assert result.shadow_llm.verdict.action == "ABSTAIN"

    def test_l2_shadow_skipped_when_red(
        self, case, event, registry, declare_done_provider,
    ):
        bundle = load_bundle()
        shadow = ShadowLLM(provider=StubLLMShadowProvider(), bundle=bundle)
        decision = ComplianceDecision(status=ShadowStatus.RED)
        result = run_agent_step(
            case=case, skill_name="email-order-entry",
            current_event=event, tool_registry=registry,
            llm_provider=declare_done_provider,
            deterministic_decision=decision,
            financial_impact_usd=1_000_000.0,
            shadow_llm=shadow,
        )
        assert result.shadow_llm is not None
        assert result.shadow_llm.verdict is None  # RED short-circuit
        assert result.shadow_llm.skip_reason == "deterministic_red_short_circuit"


# ---------------------------------------------------------------------------
# Routing predicate
# ---------------------------------------------------------------------------

class TestRoutingPredicate:
    def test_disabled_by_default(self, event):
        assert should_route_to_case_agent(event) is False
        assert should_route_to_case_agent(event, enabled=False) is False

    def test_enabled_routes_email_order_entry(self, event):
        assert should_route_to_case_agent(event, enabled=True) is True

    def test_enabled_does_not_route_other_event_types(self):
        ev = OrderEvent(
            order_id="PO-1",
            po_price=120.0, sap_base_price=120.0,
            event_type="EDI_850_PRICE_MISMATCH",
        )
        assert should_route_to_case_agent(ev, enabled=True) is False

    def test_routable_set_documents_intent(self):
        # The set is small on purpose — Manual-Order events only
        # for the Phase H.5 cutover.
        assert ROUTABLE_EVENT_TYPES == frozenset({"EMAIL_ORDER_ENTRY_REQUEST"})
