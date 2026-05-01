"""ADR-027 Phase B — per-node executed-trace evidence.

Each orchestration node appends an `ExecutedNode` entry to
`state.execution_trace` as it runs. `build_analysis` (or the trace
write site in api/routes/exceptions.py) serialises the list into
`trace_data["executed_nodes"]`, where it surfaces on
`TraceResponse.executed_nodes` and feeds the UI's EventsTimeline +
PipelineDAG. Reanalysis preserves prior attempts on
`ReanalysisHistoryEntry.executed_nodes`.
"""
from __future__ import annotations

import pytest

pytest.importorskip("langgraph", reason="langgraph not installed")

from contracts.models import (  # noqa: E402
    ExecutedNode,
    GraphState,
    OrderEvent,
    TerminalStatus,
)
from orchestration.graph import run_graph  # noqa: E402


def _state(**kwargs) -> GraphState:
    defaults = dict(order_id="SO-T-EXEC", po_price=90.0, sap_base_price=100.0)
    defaults.update(kwargs)
    return GraphState(event=OrderEvent(**defaults))


class TestExecutionTracePopulated:
    def test_initial_state_has_empty_execution_trace(self):
        state = _state()
        assert state.execution_trace == []

    def test_run_graph_populates_execution_trace(self):
        result = run_graph(_state())
        assert len(result.execution_trace) >= 2
        assert all(isinstance(n, ExecutedNode) for n in result.execution_trace)

    def test_first_node_is_ingest(self):
        result = run_graph(_state())
        assert result.execution_trace[0].node == "ingest"

    def test_terminal_node_is_build_analysis_or_halt(self):
        result = run_graph(_state())
        last = result.execution_trace[-1]
        # Every path lands on build_analysis (Verdict Pillar 2) unless
        # ingest itself errored. For pricing fixtures we always reach
        # build_analysis.
        assert last.node == "build_analysis"

    def test_each_executed_node_has_iso_timestamps(self):
        result = run_graph(_state())
        for n in result.execution_trace:
            assert n.entered_at
            assert n.timestamp == n.entered_at
            assert n.completed_at is not None or n.status == "errored"

    def test_each_executed_node_has_status(self):
        result = run_graph(_state())
        for n in result.execution_trace:
            assert n.status in {"completed", "halted", "errored"}

    def test_duration_ms_is_non_negative(self):
        result = run_graph(_state())
        for n in result.execution_trace:
            if n.duration_ms is not None:
                assert n.duration_ms >= 0


class TestExitVerdicts:
    def test_classify_emits_ok_verdict_on_success(self):
        result = run_graph(_state())
        classify = next(
            n for n in result.execution_trace if n.node == "classify"
        )
        assert classify.exit_verdict == "ok"
        assert "intent" in classify.decision

    def test_validate_circuit_breaker_emits_ok_or_breach(self):
        result = run_graph(_state())
        cb = next(
            n for n in result.execution_trace
            if n.node == "validate_circuit_breaker"
        )
        assert cb.exit_verdict in {"ok", "breach"}

    def test_select_recipe_emits_ok_or_no_recipe(self):
        result = run_graph(_state())
        sr = next(
            n for n in result.execution_trace if n.node == "select_recipe"
        )
        assert sr.exit_verdict in {"ok", "no_recipe"}

    def test_circuit_breaker_emits_one_of_known_verdicts(self):
        # Default fixture is well under the breaker thresholds, so the
        # verdict is "ok". Just assert the contract that whatever fires
        # is in the registered vocabulary.
        result = run_graph(_state())
        cb = next(
            n for n in result.execution_trace
            if n.node == "validate_circuit_breaker"
        )
        assert cb.exit_verdict in {"ok", "breach"}


class TestShadowAuditPolicyHits:
    def test_shadow_audit_records_status_and_policy_hits(self):
        result = run_graph(_state())
        shadow = next(
            (n for n in result.execution_trace if n.node == "shadow_audit"),
            None,
        )
        if shadow is None:
            pytest.skip("shadow_audit did not run for this fixture")
        assert shadow.exit_verdict in {"green", "yellow", "red"}
        assert "shadow_status" in shadow.decision
        # policy_hits is always a list (may be empty)
        assert isinstance(shadow.policy_hits, list)


class TestSubSpansOnResolveDependencies:
    def test_resolve_dependencies_carries_sub_spans_when_recipe_has_deps(self):
        # PriceAdjustmentRecipe (the typical pricing flow) doesn't carry
        # gateway dependencies in the default test config, so this test
        # is conditional. The contract under test: IF sub_spans are
        # populated, every entry has a gateway name + status.
        result = run_graph(_state())
        rd = next(
            (n for n in result.execution_trace
             if n.node == "resolve_dependencies"),
            None,
        )
        if rd is None or not rd.sub_spans:
            pytest.skip("no gateway dependencies for this fixture")
        for span in rd.sub_spans:
            assert span.gateway
            assert span.status in {"ok", "error", "timeout"}


class TestNonOrchestrationNodesHaveNoExitVerdict:
    """ingest/load_skill/execute_recipe/apply_effects don't drive a
    conditional gate, so their exit_verdict is None."""

    def test_ingest_has_no_exit_verdict(self):
        result = run_graph(_state())
        ingest = result.execution_trace[0]
        assert ingest.node == "ingest"
        assert ingest.exit_verdict is None

    def test_load_skill_has_no_exit_verdict(self):
        result = run_graph(_state())
        ls = next(n for n in result.execution_trace if n.node == "load_skill")
        assert ls.exit_verdict is None


class TestCrossCheckDisagreementVerdict:
    """When the cross-check disagrees, classify itself emits
    exit_verdict='cross_check_disagreement' (the implicit gate
    registered in _IMPLICIT_VERDICT_LABELS)."""

    def test_classify_sets_disagreement_verdict_on_disagreement(self, monkeypatch):
        from constraints.cross_check import CrossCheckResult
        from constraints import fallback_backend
        import orchestration.nodes as nodes

        # Monkeypatch _backend to return a non-fallback "LLM-like" backend
        # whose intent disagrees with the deterministic outcome.
        class FakeLLMBackend:
            def classify_intent(self, state):
                # Pick any allowed intent that's not what the deterministic
                # fallback returns for the test fixture.
                return fallback_backend.IntentDecision(
                    intent="DUPLICATE_PO", confidence=0.99,
                )
        monkeypatch.setattr(nodes, "_backend", lambda task=None: FakeLLMBackend())
        # Force a disagreement-shaped CrossCheckResult.
        def fake_cross_check(*, llm_decision, deterministic_decision):
            return CrossCheckResult(
                agreed=False,
                llm_intent=llm_decision.intent,
                deterministic_intent=deterministic_decision.intent,
                winning_decision=deterministic_decision,
                reason="forced disagreement for test",
            )
        monkeypatch.setattr(nodes, "cross_check", fake_cross_check)

        result = run_graph(_state())
        classify = next(
            n for n in result.execution_trace if n.node == "classify"
        )
        assert classify.exit_verdict == "cross_check_disagreement"
        assert classify.status == "halted"
        # final_status may be MANUAL_REVIEW_REQUIRED or upgraded to
        # AUDIT_CONTEXT_MISSING by build_analysis if the registry
        # coverage check fails — both are valid terminal states for
        # a cross-check halt; the test under verification is the
        # classify-level verdict, not the eventual lifecycle.
        assert result.final_status in {
            TerminalStatus.MANUAL_REVIEW_REQUIRED,
            TerminalStatus.AUDIT_CONTEXT_MISSING,
        }


class TestTracePersistenceOnResolve:
    """The /resolve write site must serialise execution_trace into
    trace_data['executed_nodes']."""

    @pytest.fixture()
    def client(self):
        from fastapi.testclient import TestClient
        from api.app import create_app
        return TestClient(create_app(), raise_server_exceptions=False)

    @pytest.fixture()
    def auth_header(self):
        from api.deps import create_test_token
        return {"Authorization": f"Bearer {create_test_token()}"}

    def test_resolve_then_get_trace_returns_executed_nodes(self, client, auth_header):
        body = {
            "order_id": "SO-EXEC-RESOLVE-1",
            "po_price": 90.0,
            "sap_base_price": 100.0,
            "event_type": "ORDER",
        }
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=body,
            headers=auth_header,
        )
        assert r.status_code == 200, r.text
        exception_id = r.json()["exception_id"]
        rt = client.get(
            f"/api/v1/exceptions/{exception_id}/trace",
            headers=auth_header,
        )
        assert rt.status_code == 200, rt.text
        body = rt.json()
        assert "executed_nodes" in body
        assert len(body["executed_nodes"]) >= 2
        # First ExecutedNode is ingest.
        assert body["executed_nodes"][0]["node"] == "ingest"
