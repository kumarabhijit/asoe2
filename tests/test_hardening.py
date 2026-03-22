from __future__ import annotations

# Phase 6 — Hardening tests
#
# Covers:
#   Kill switch
#     - is_kill_switch_active() reads env var correctly (truthy/falsy)
#     - apply_kill_switch() returns FAIL_TO_HUMAN without running nodes
#     - run_graph() respects kill switch (langgraph-gated)
#     - kill-switch explanation string is set
#     - no shadow, no execution_log when kill switch fires
#   Explain mode
#     - is_explain_mode_active() reads env var correctly
#     - explain_only node sets MANUAL_REVIEW_REQUIRED
#     - explain_only node records intent, shadow verdict, recipe in explanation
#     - run_graph() with ASOE_EXPLAIN_MODE=1 uses explain_only (langgraph-gated)
#     - shadow still runs in explain mode (real verdict in explanation)
#     - circuit breaker still fires in explain mode
#     - no execution_log when explain mode halts at explain_only
#   build_explain_summary()
#     - all fields present in summary text
#     - missing optional fields handled gracefully
#   Invariants
#     - hardening module does not import langfuse
#     - apply_kill_switch does not mutate input state
#     - explain_only does not call RecipeExecutor

import os
from unittest.mock import patch

import pytest

from contracts.models import (
    ComplianceDecision,
    GraphState,
    Intent,
    OrderEvent,
    RecipeInvocation,
    ShadowStatus,
    TerminalStatus,
)
from hardening.kill_switch import (
    KILL_SWITCH_EXPLANATION,
    apply_kill_switch,
    is_kill_switch_active,
)
from hardening.explain_mode import build_explain_summary, is_explain_mode_active
from orchestration.nodes import explain_only


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _state(**kwargs) -> GraphState:
    defaults = dict(order_id="SO-H001", po_price=90.0, sap_base_price=100.0)
    defaults.update(kwargs)
    return GraphState(event=OrderEvent(**defaults))


def _green_shadow() -> ComplianceDecision:
    return ComplianceDecision(
        status=ShadowStatus.GREEN,
        reasons=["ok"],
        policy_hits=[],
        constrained_by="fallback",
    )


# ---------------------------------------------------------------------------
# Kill switch — unit tests (no langgraph required)
# ---------------------------------------------------------------------------

class TestKillSwitchEnvVar:
    def test_inactive_by_default(self, monkeypatch):
        monkeypatch.delenv("ASOE_KILL_SWITCH", raising=False)
        assert is_kill_switch_active() is False

    def test_active_when_set_to_1(self, monkeypatch):
        monkeypatch.setenv("ASOE_KILL_SWITCH", "1")
        assert is_kill_switch_active() is True

    def test_active_when_set_to_true(self, monkeypatch):
        monkeypatch.setenv("ASOE_KILL_SWITCH", "true")
        assert is_kill_switch_active() is True

    def test_active_when_set_to_yes(self, monkeypatch):
        monkeypatch.setenv("ASOE_KILL_SWITCH", "yes")
        assert is_kill_switch_active() is True

    def test_active_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("ASOE_KILL_SWITCH", "TRUE")
        assert is_kill_switch_active() is True

    def test_inactive_when_set_to_0(self, monkeypatch):
        monkeypatch.setenv("ASOE_KILL_SWITCH", "0")
        assert is_kill_switch_active() is False

    def test_inactive_when_set_to_false(self, monkeypatch):
        monkeypatch.setenv("ASOE_KILL_SWITCH", "false")
        assert is_kill_switch_active() is False

    def test_inactive_when_set_to_no(self, monkeypatch):
        monkeypatch.setenv("ASOE_KILL_SWITCH", "no")
        assert is_kill_switch_active() is False


class TestApplyKillSwitch:
    def test_returns_fail_to_human(self):
        state = _state()
        result = apply_kill_switch(state)
        assert result.final_status == TerminalStatus.FAIL_TO_HUMAN

    def test_explanation_is_set(self):
        state = _state()
        result = apply_kill_switch(state)
        assert result.explanation == KILL_SWITCH_EXPLANATION

    def test_explanation_mentions_kill_switch(self):
        state = _state()
        result = apply_kill_switch(state)
        assert "ASOE_KILL_SWITCH" in result.explanation

    def test_does_not_mutate_input_state(self):
        state = _state()
        before_status = state.final_status
        apply_kill_switch(state)
        assert state.final_status == before_status  # original unchanged

    def test_returns_graph_state(self):
        state = _state()
        result = apply_kill_switch(state)
        assert isinstance(result, GraphState)

    def test_event_preserved(self):
        state = _state()
        result = apply_kill_switch(state)
        assert result.event.order_id == state.event.order_id

    def test_no_execution_log(self):
        state = _state()
        result = apply_kill_switch(state)
        assert result.execution_log is None

    def test_no_shadow_decision(self):
        state = _state()
        result = apply_kill_switch(state)
        assert result.shadow is None


# ---------------------------------------------------------------------------
# Kill switch — graph-level (langgraph required)
# ---------------------------------------------------------------------------

class TestKillSwitchGraph:
    pytestmark = pytest.mark.skipif(
        pytest.importorskip("langgraph", reason="langgraph not installed") is None,
        reason="langgraph not installed",
    )

    def test_run_graph_respects_kill_switch(self, monkeypatch, pricing_event):
        pytest.importorskip("langgraph")
        from orchestration.graph import run_graph
        monkeypatch.setenv("ASOE_KILL_SWITCH", "1")
        result = run_graph(pricing_event)
        assert result.final_status == TerminalStatus.FAIL_TO_HUMAN

    def test_run_graph_kill_switch_no_shadow(self, monkeypatch, pricing_event):
        pytest.importorskip("langgraph")
        from orchestration.graph import run_graph
        monkeypatch.setenv("ASOE_KILL_SWITCH", "1")
        result = run_graph(pricing_event)
        assert result.shadow is None

    def test_run_graph_kill_switch_no_execution_log(self, monkeypatch, pricing_event):
        pytest.importorskip("langgraph")
        from orchestration.graph import run_graph
        monkeypatch.setenv("ASOE_KILL_SWITCH", "1")
        result = run_graph(pricing_event)
        assert result.execution_log is None

    def test_run_graph_kill_switch_explanation_set(self, monkeypatch, pricing_event):
        pytest.importorskip("langgraph")
        from orchestration.graph import run_graph
        monkeypatch.setenv("ASOE_KILL_SWITCH", "1")
        result = run_graph(pricing_event)
        assert result.explanation is not None
        assert "ASOE_KILL_SWITCH" in result.explanation

    def test_run_graph_no_kill_switch_still_completes(self, monkeypatch, pricing_event):
        pytest.importorskip("langgraph")
        from orchestration.graph import run_graph
        monkeypatch.delenv("ASOE_KILL_SWITCH", raising=False)
        monkeypatch.delenv("ASOE_EXPLAIN_MODE", raising=False)
        result = run_graph(pricing_event)
        assert result.final_status == TerminalStatus.COMPLETE


# ---------------------------------------------------------------------------
# Explain mode — env var unit tests
# ---------------------------------------------------------------------------

class TestExplainModeEnvVar:
    def test_inactive_by_default(self, monkeypatch):
        monkeypatch.delenv("ASOE_EXPLAIN_MODE", raising=False)
        assert is_explain_mode_active() is False

    def test_active_when_set_to_1(self, monkeypatch):
        monkeypatch.setenv("ASOE_EXPLAIN_MODE", "1")
        assert is_explain_mode_active() is True

    def test_active_when_set_to_true(self, monkeypatch):
        monkeypatch.setenv("ASOE_EXPLAIN_MODE", "true")
        assert is_explain_mode_active() is True

    def test_active_when_set_to_yes(self, monkeypatch):
        monkeypatch.setenv("ASOE_EXPLAIN_MODE", "yes")
        assert is_explain_mode_active() is True

    def test_inactive_when_set_to_0(self, monkeypatch):
        monkeypatch.setenv("ASOE_EXPLAIN_MODE", "0")
        assert is_explain_mode_active() is False

    def test_inactive_when_set_to_false(self, monkeypatch):
        monkeypatch.setenv("ASOE_EXPLAIN_MODE", "false")
        assert is_explain_mode_active() is False


# ---------------------------------------------------------------------------
# explain_only node — unit tests (no langgraph required)
# ---------------------------------------------------------------------------

class TestExplainOnlyNode:
    def _explain_state(self) -> GraphState:
        state = _state(po_price=92.0, sap_base_price=100.0)
        state.intent = Intent.CONTRACTUAL_CORRECTION
        state.shadow = _green_shadow()
        state.selected_recipe = "PriceAdjustmentRecipe.py"
        state.invocation = RecipeInvocation(
            recipe_name="PriceAdjustmentRecipe.py",
            params={
                "order_id": "SO-H001",
                "line_item": 1,
                "requested_price": 92.0,
                "erp_context": {"base_price": 100.0, "max_discount_allowed": 0.15, "condition_type": "YK07"},
            },
        )
        return state

    def test_sets_manual_review_required(self):
        result = explain_only(self._explain_state())
        assert result.final_status == TerminalStatus.MANUAL_REVIEW_REQUIRED

    def test_explanation_is_set(self):
        result = explain_only(self._explain_state())
        assert result.explanation is not None and len(result.explanation) > 0

    def test_explanation_mentions_explain_mode(self):
        result = explain_only(self._explain_state())
        assert "EXPLAIN MODE" in result.explanation

    def test_explanation_contains_intent(self):
        result = explain_only(self._explain_state())
        assert "CONTRACTUAL_CORRECTION" in result.explanation

    def test_explanation_contains_shadow_verdict(self):
        result = explain_only(self._explain_state())
        assert "GREEN" in result.explanation

    def test_explanation_contains_recipe_name(self):
        result = explain_only(self._explain_state())
        assert "PriceAdjustmentRecipe.py" in result.explanation

    def test_no_execution_log(self):
        result = explain_only(self._explain_state())
        assert result.execution_log is None

    def test_returns_graph_state(self):
        result = explain_only(self._explain_state())
        assert isinstance(result, GraphState)

    def test_shadow_decision_preserved(self):
        state = self._explain_state()
        result = explain_only(state)
        assert result.shadow is not None
        assert result.shadow.status == ShadowStatus.GREEN

    def test_no_shadow_no_crash(self):
        """explain_only must not crash when shadow has not run yet."""
        state = _state()
        state.selected_recipe = "PriceAdjustmentRecipe.py"
        result = explain_only(state)
        assert result.final_status == TerminalStatus.MANUAL_REVIEW_REQUIRED

    def test_policy_hits_in_explanation(self):
        state = _state()
        state.intent = Intent.CREDIT_BLOCK
        state.shadow = ComplianceDecision(
            status=ShadowStatus.YELLOW,
            reasons=["credit review"],
            policy_hits=["CREDIT_RELEASE_REVIEW"],
            constrained_by="fallback",
        )
        state.selected_recipe = "CreditHoldReleaseRecipe.py"
        result = explain_only(state)
        assert "CREDIT_RELEASE_REVIEW" in result.explanation


# ---------------------------------------------------------------------------
# Explain mode — graph-level (langgraph required)
# ---------------------------------------------------------------------------

class TestExplainModeGraph:
    def test_explain_mode_returns_manual_review(self, monkeypatch, pricing_event):
        pytest.importorskip("langgraph")
        from orchestration.graph import run_graph
        monkeypatch.setenv("ASOE_EXPLAIN_MODE", "1")
        monkeypatch.delenv("ASOE_KILL_SWITCH", raising=False)
        result = run_graph(pricing_event)
        assert result.final_status == TerminalStatus.MANUAL_REVIEW_REQUIRED

    def test_explain_mode_no_execution_log(self, monkeypatch, pricing_event):
        pytest.importorskip("langgraph")
        from orchestration.graph import run_graph
        monkeypatch.setenv("ASOE_EXPLAIN_MODE", "1")
        monkeypatch.delenv("ASOE_KILL_SWITCH", raising=False)
        result = run_graph(pricing_event)
        assert result.execution_log is None

    def test_explain_mode_shadow_still_runs(self, monkeypatch, pricing_event):
        """The Compliance Shadow DOES run in explain mode."""
        pytest.importorskip("langgraph")
        from orchestration.graph import run_graph
        monkeypatch.setenv("ASOE_EXPLAIN_MODE", "1")
        monkeypatch.delenv("ASOE_KILL_SWITCH", raising=False)
        result = run_graph(pricing_event)
        assert result.shadow is not None
        assert result.shadow.status.value == "GREEN"

    def test_explain_mode_intent_classified(self, monkeypatch, pricing_event):
        pytest.importorskip("langgraph")
        from orchestration.graph import run_graph
        monkeypatch.setenv("ASOE_EXPLAIN_MODE", "1")
        monkeypatch.delenv("ASOE_KILL_SWITCH", raising=False)
        result = run_graph(pricing_event)
        assert result.intent == Intent.CONTRACTUAL_CORRECTION

    def test_explain_mode_explanation_contains_recipe(self, monkeypatch, pricing_event):
        pytest.importorskip("langgraph")
        from orchestration.graph import run_graph
        monkeypatch.setenv("ASOE_EXPLAIN_MODE", "1")
        monkeypatch.delenv("ASOE_KILL_SWITCH", raising=False)
        result = run_graph(pricing_event)
        assert "PriceAdjustmentRecipe.py" in result.explanation

    def test_explain_mode_explanation_contains_intent(self, monkeypatch, pricing_event):
        pytest.importorskip("langgraph")
        from orchestration.graph import run_graph
        monkeypatch.setenv("ASOE_EXPLAIN_MODE", "1")
        monkeypatch.delenv("ASOE_KILL_SWITCH", raising=False)
        result = run_graph(pricing_event)
        assert "CONTRACTUAL_CORRECTION" in result.explanation

    def test_circuit_breaker_still_fires_in_explain_mode(self, monkeypatch):
        """Circuit breaker halts execution even when explain mode is active."""
        pytest.importorskip("langgraph")
        from orchestration.graph import run_graph
        monkeypatch.setenv("ASOE_EXPLAIN_MODE", "1")
        monkeypatch.delenv("ASOE_KILL_SWITCH", raising=False)
        state = _state()
        state.update_count = 51
        result = run_graph(state)
        assert result.final_status == TerminalStatus.FAIL_TO_HUMAN

    def test_kill_switch_takes_precedence_over_explain_mode(self, monkeypatch, pricing_event):
        pytest.importorskip("langgraph")
        from orchestration.graph import run_graph
        monkeypatch.setenv("ASOE_KILL_SWITCH", "1")
        monkeypatch.setenv("ASOE_EXPLAIN_MODE", "1")
        result = run_graph(pricing_event)
        assert result.final_status == TerminalStatus.FAIL_TO_HUMAN
        assert "ASOE_KILL_SWITCH" in result.explanation


# ---------------------------------------------------------------------------
# build_explain_summary() unit tests
# ---------------------------------------------------------------------------

class TestBuildExplainSummary:
    def test_contains_explain_mode_header(self):
        s = build_explain_summary("CONTRACTUAL_CORRECTION", "GREEN", [], "PriceAdjustmentRecipe.py", {})
        assert "EXPLAIN MODE" in s

    def test_contains_intent(self):
        s = build_explain_summary("CONTRACTUAL_CORRECTION", "GREEN", [], None, None)
        assert "CONTRACTUAL_CORRECTION" in s

    def test_contains_shadow_verdict(self):
        s = build_explain_summary("CONTRACTUAL_CORRECTION", "GREEN", [], None, None)
        assert "GREEN" in s

    def test_contains_recipe_name(self):
        s = build_explain_summary("CONTRACTUAL_CORRECTION", "GREEN", [], "PriceAdjustmentRecipe.py", None)
        assert "PriceAdjustmentRecipe.py" in s

    def test_contains_policy_hits(self):
        s = build_explain_summary("CREDIT_BLOCK", "YELLOW", ["CREDIT_RELEASE_REVIEW"], None, None)
        assert "CREDIT_RELEASE_REVIEW" in s

    def test_no_recipe_handled_gracefully(self):
        s = build_explain_summary("MASS_PRICING_ERROR", "RED", [], None, None)
        assert s is not None and len(s) > 0

    def test_no_shadow_handled_gracefully(self):
        s = build_explain_summary(None, None, [], None, None)
        assert "EXPLAIN MODE" in s

    def test_params_included_when_present(self):
        params = {"order_id": "SO-1", "requested_price": 90.0}
        s = build_explain_summary("CONTRACTUAL_CORRECTION", "GREEN", [], "PriceAdjustmentRecipe.py", params)
        assert "SO-1" in s

    def test_activate_hint_included(self):
        s = build_explain_summary("CONTRACTUAL_CORRECTION", "GREEN", [], "PriceAdjustmentRecipe.py", None)
        assert "ASOE_EXPLAIN_MODE" in s


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------

class TestHardeningInvariants:
    def test_kill_switch_module_does_not_import_langfuse(self):
        import hardening.kill_switch as mod
        assert "langfuse" not in dir(mod)

    def test_explain_mode_module_does_not_import_langfuse(self):
        import hardening.explain_mode as mod
        assert "langfuse" not in dir(mod)

    def test_explain_only_does_not_call_recipe_executor(self):
        """explain_only must never import or call RecipeExecutor."""
        import orchestration.nodes as nodes_mod
        import inspect
        source = inspect.getsource(nodes_mod.explain_only)
        assert "RecipeExecutor" not in source

    def test_apply_kill_switch_returns_new_object(self):
        state = _state()
        result = apply_kill_switch(state)
        assert result is not state
