from __future__ import annotations

# Phase 5 — Observability / Tracer unit tests
#
# Covers:
#   TraceRecord construction and validation
#   Tracer.build_record() field extraction from GraphState
#   JSON serialisation (LangFuse-ready payload)
#   emit() writes to stdlib logger (no langfuse import)
#   Defensive extraction: partial state (no shadow, no execution_log, etc.)
#   extra="forbid" on TraceRecord
#   Invariants: tracer never imports langfuse, never calls live LLM

import json
import logging
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from contracts.models import (
    ComplianceDecision,
    ExecutionLog,
    GraphState,
    Intent,
    OrderEvent,
    ShadowStatus,
    TerminalStatus,
)
from observability.tracer import TraceRecord, Tracer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_state() -> GraphState:
    return GraphState(event=OrderEvent(order_id="SO-OBS-001", po_price=90.0, sap_base_price=100.0))


def _green_shadow() -> ComplianceDecision:
    return ComplianceDecision(
        status=ShadowStatus.GREEN,
        reasons=["ok"],
        policy_hits=[],
        constrained_by="Guidance/Outlines fallback schema",
    )


def _yellow_shadow() -> ComplianceDecision:
    return ComplianceDecision(
        status=ShadowStatus.YELLOW,
        reasons=["credit review needed"],
        policy_hits=["CREDIT_RELEASE_REVIEW"],
        constrained_by="Guidance/Outlines fallback schema",
    )


def _red_shadow() -> ComplianceDecision:
    return ComplianceDecision(
        status=ShadowStatus.RED,
        reasons=["mass error"],
        policy_hits=["MASS_ERROR_POLICY"],
        constrained_by="Guidance/Outlines fallback schema",
    )


def _execution_log(trace_id: str, recipe_name: str = "PriceAdjustmentRecipe.py") -> ExecutionLog:
    return ExecutionLog(
        trace_id=trace_id,
        recipe_name=recipe_name,
        inputs={"order_id": "SO-OBS-001"},
        outputs={"status": "SUCCESS", "applied_condition": "YK07"},
        errors=[],
        constrained_outputs={
            "intent": "IntentDecision",
            "shadow": "ShadowDecisionSchema",
            "recipe": "RecipeProposal",
        },
        intent_selected="CONTRACTUAL_CORRECTION",
    )


# ---------------------------------------------------------------------------
# TraceRecord model validation
# ---------------------------------------------------------------------------

class TestTraceRecordModel:
    def test_minimal_valid_record(self):
        record = TraceRecord(trace_id="tid-1", event_id="SO-1")
        assert record.trace_id == "tid-1"
        assert record.event_id == "SO-1"

    def test_all_fields_accepted(self):
        record = TraceRecord(
            trace_id="tid-2",
            event_id="SO-2",
            skill_name="pricing-reconciliation",
            intent_selected="CONTRACTUAL_CORRECTION",
            shadow_verdict="GREEN",
            shadow_policy_hits=[],
            recipe_name="PriceAdjustmentRecipe.py",
            rag_chunks=[],
            constrained_output_schemas={"intent": "IntentDecision"},
            final_status="COMPLETE",
            explanation="ok",
        )
        assert record.skill_name == "pricing-reconciliation"
        assert record.final_status == "COMPLETE"

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            TraceRecord(trace_id="t", event_id="e", unknown_field="x")

    def test_shadow_policy_hits_defaults_to_empty_list(self):
        record = TraceRecord(trace_id="t", event_id="e")
        assert record.shadow_policy_hits == []

    def test_rag_chunks_defaults_to_empty_list(self):
        record = TraceRecord(trace_id="t", event_id="e")
        assert record.rag_chunks == []

    def test_constrained_output_schemas_defaults_to_empty_dict(self):
        record = TraceRecord(trace_id="t", event_id="e")
        assert record.constrained_output_schemas == {}

    def test_optional_fields_default_to_none(self):
        record = TraceRecord(trace_id="t", event_id="e")
        assert record.skill_name is None
        assert record.intent_selected is None
        assert record.shadow_verdict is None
        assert record.recipe_name is None
        assert record.final_status is None
        assert record.explanation is None


# ---------------------------------------------------------------------------
# JSON serialisation
# ---------------------------------------------------------------------------

class TestTraceRecordJSON:
    def test_to_json_returns_string(self):
        record = TraceRecord(trace_id="t", event_id="e")
        assert isinstance(record.to_json(), str)

    def test_to_json_is_parseable(self):
        record = TraceRecord(trace_id="t", event_id="e", final_status="COMPLETE")
        parsed = json.loads(record.to_json())
        assert parsed["trace_id"] == "t"
        assert parsed["final_status"] == "COMPLETE"

    def test_to_json_contains_all_required_fields(self):
        record = TraceRecord(trace_id="t", event_id="e")
        parsed = json.loads(record.to_json())
        for key in ("trace_id", "event_id", "skill_name", "intent_selected",
                    "shadow_verdict", "shadow_policy_hits", "recipe_name",
                    "rag_chunks", "constrained_output_schemas", "final_status", "explanation"):
            assert key in parsed, f"Missing key: {key}"

    def test_to_json_policy_hits_serialised_as_list(self):
        record = TraceRecord(trace_id="t", event_id="e", shadow_policy_hits=["P1", "P2"])
        parsed = json.loads(record.to_json())
        assert parsed["shadow_policy_hits"] == ["P1", "P2"]


# ---------------------------------------------------------------------------
# Tracer.build_record() — field extraction
# ---------------------------------------------------------------------------

class TestTracerBuildRecord:
    def test_event_id_extracted(self):
        state = _minimal_state()
        record = Tracer().build_record(state)
        assert record.event_id == "SO-OBS-001"

    def test_trace_id_from_execution_log(self):
        state = _minimal_state()
        state.shadow = _green_shadow()
        state.execution_log = _execution_log(trace_id=state.shadow.trace_id)
        record = Tracer().build_record(state)
        assert record.trace_id == state.execution_log.trace_id

    def test_trace_id_falls_back_to_shadow(self):
        state = _minimal_state()
        state.shadow = _green_shadow()
        # no execution_log
        record = Tracer().build_record(state)
        assert record.trace_id == state.shadow.trace_id

    def test_trace_id_empty_when_no_shadow_or_log(self):
        state = _minimal_state()
        record = Tracer().build_record(state)
        assert record.trace_id == ""

    def test_skill_name_extracted(self, pricing_event):
        from orchestration.nodes import load_skill
        state = load_skill(pricing_event)
        record = Tracer().build_record(state)
        assert record.skill_name == "pricing-reconciliation"

    def test_skill_name_none_when_not_loaded(self):
        state = _minimal_state()
        record = Tracer().build_record(state)
        assert record.skill_name is None

    def test_intent_extracted(self):
        state = _minimal_state()
        state.intent = Intent.CONTRACTUAL_CORRECTION
        record = Tracer().build_record(state)
        assert record.intent_selected == "CONTRACTUAL_CORRECTION"

    def test_intent_none_when_not_set(self):
        state = _minimal_state()
        record = Tracer().build_record(state)
        assert record.intent_selected is None

    def test_shadow_verdict_green(self):
        state = _minimal_state()
        state.shadow = _green_shadow()
        record = Tracer().build_record(state)
        assert record.shadow_verdict == "GREEN"

    def test_shadow_verdict_yellow(self):
        state = _minimal_state()
        state.shadow = _yellow_shadow()
        record = Tracer().build_record(state)
        assert record.shadow_verdict == "YELLOW"

    def test_shadow_verdict_red(self):
        state = _minimal_state()
        state.shadow = _red_shadow()
        record = Tracer().build_record(state)
        assert record.shadow_verdict == "RED"

    def test_shadow_verdict_none_when_no_shadow(self):
        state = _minimal_state()
        record = Tracer().build_record(state)
        assert record.shadow_verdict is None

    def test_shadow_policy_hits_extracted(self):
        state = _minimal_state()
        state.shadow = _yellow_shadow()
        record = Tracer().build_record(state)
        assert "CREDIT_RELEASE_REVIEW" in record.shadow_policy_hits

    def test_shadow_policy_hits_empty_for_green(self):
        state = _minimal_state()
        state.shadow = _green_shadow()
        record = Tracer().build_record(state)
        assert record.shadow_policy_hits == []

    def test_recipe_name_from_execution_log(self):
        state = _minimal_state()
        state.shadow = _green_shadow()
        state.execution_log = _execution_log(trace_id=state.shadow.trace_id)
        record = Tracer().build_record(state)
        assert record.recipe_name == "PriceAdjustmentRecipe.py"

    def test_recipe_name_falls_back_to_selected_recipe(self):
        state = _minimal_state()
        state.selected_recipe = "CreditHoldReleaseRecipe.py"
        record = Tracer().build_record(state)
        assert record.recipe_name == "CreditHoldReleaseRecipe.py"

    def test_recipe_name_none_when_no_recipe(self):
        state = _minimal_state()
        record = Tracer().build_record(state)
        assert record.recipe_name is None

    def test_constrained_output_schemas_from_execution_log(self):
        state = _minimal_state()
        state.shadow = _green_shadow()
        state.execution_log = _execution_log(trace_id=state.shadow.trace_id)
        record = Tracer().build_record(state)
        assert record.constrained_output_schemas.get("intent") == "IntentDecision"
        assert record.constrained_output_schemas.get("shadow") == "ShadowDecisionSchema"
        assert record.constrained_output_schemas.get("recipe") == "RecipeProposal"

    def test_final_status_extracted(self):
        state = _minimal_state()
        state.final_status = TerminalStatus.COMPLETE
        record = Tracer().build_record(state)
        assert record.final_status == "COMPLETE"

    def test_final_status_none_when_not_set(self):
        state = _minimal_state()
        record = Tracer().build_record(state)
        assert record.final_status is None

    def test_explanation_extracted(self):
        state = _minimal_state()
        state.explanation = "All good"
        record = Tracer().build_record(state)
        assert record.explanation == "All good"

    def test_returns_trace_record_instance(self):
        state = _minimal_state()
        record = Tracer().build_record(state)
        assert isinstance(record, TraceRecord)


# ---------------------------------------------------------------------------
# Tracer.emit() — stdlib logging, no langfuse
# ---------------------------------------------------------------------------

class TestTracerEmit:
    def test_emit_calls_logger(self, caplog):
        record = TraceRecord(trace_id="tid-emit", event_id="SO-E", final_status="COMPLETE")
        with caplog.at_level(logging.INFO, logger="asoe.observability"):
            Tracer().emit(record)
        assert len(caplog.records) == 1

    def test_emit_log_contains_trace_key(self, caplog):
        record = TraceRecord(trace_id="tid-json", event_id="SO-J")
        with caplog.at_level(logging.INFO, logger="asoe.observability"):
            Tracer().emit(record)
        payload = json.loads(caplog.records[0].message)
        assert "trace" in payload

    def test_emit_log_trace_id_correct(self, caplog):
        record = TraceRecord(trace_id="tid-check", event_id="SO-C")
        with caplog.at_level(logging.INFO, logger="asoe.observability"):
            Tracer().emit(record)
        payload = json.loads(caplog.records[0].message)
        assert payload["trace"]["trace_id"] == "tid-check"

    def test_emit_log_final_status_correct(self, caplog):
        record = TraceRecord(trace_id="t", event_id="e", final_status="FAIL_TO_HUMAN")
        with caplog.at_level(logging.INFO, logger="asoe.observability"):
            Tracer().emit(record)
        payload = json.loads(caplog.records[0].message)
        assert payload["trace"]["final_status"] == "FAIL_TO_HUMAN"


# ---------------------------------------------------------------------------
# Invariants — no langfuse import, no live LLM call
# ---------------------------------------------------------------------------

class TestTracerInvariants:
    def test_tracer_module_does_not_import_langfuse(self):
        import observability.tracer as mod
        assert not hasattr(mod, "langfuse"), "tracer must not import langfuse at module level"

    def test_trace_record_module_does_not_import_langfuse(self):
        import observability.tracer as mod
        # langfuse should not appear in the module's globals
        assert "langfuse" not in dir(mod)

    def test_build_record_does_not_raise_on_empty_state(self):
        state = _minimal_state()
        record = Tracer().build_record(state)
        assert record is not None

    def test_build_record_does_not_mutate_state(self):
        state = _minimal_state()
        before_status = state.final_status
        Tracer().build_record(state)
        assert state.final_status == before_status


# ---------------------------------------------------------------------------
# LangFuse sink — observability/langfuse_sink.py
# ---------------------------------------------------------------------------

from observability.langfuse_sink import forward, reset_client


class TestLangFuseSinkDisabled:
    """LangFuse sink is a no-op when keys are not set or package is missing."""

    def setup_method(self):
        reset_client()

    def teardown_method(self):
        reset_client()

    def test_forward_returns_false_when_keys_not_set(self, monkeypatch):
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        record = TraceRecord(trace_id="t", event_id="e", final_status="COMPLETE")
        assert forward(record) is False

    def test_forward_returns_false_when_public_key_empty(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        record = TraceRecord(trace_id="t", event_id="e")
        assert forward(record) is False

    def test_forward_returns_false_when_secret_key_empty(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
        record = TraceRecord(trace_id="t", event_id="e")
        assert forward(record) is False

    def test_forward_returns_false_when_langfuse_import_fails(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        # Simulate langfuse not installed by patching the import
        import builtins
        real_import = builtins.__import__

        def _block_langfuse(name, *args, **kwargs):
            if name == "langfuse":
                raise ImportError("no langfuse")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _block_langfuse)
        record = TraceRecord(trace_id="t", event_id="e")
        assert forward(record) is False

    def test_reset_client_allows_reinitialisation(self, monkeypatch):
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        record = TraceRecord(trace_id="t", event_id="e")
        assert forward(record) is False
        # After reset, the client re-checks env vars
        reset_client()
        assert forward(record) is False  # still no keys


def _make_mock_client():
    """Build a mock LangFuse v4 client with call recording.

    Returns (client, observations, scores) where observations and scores
    are lists that accumulate kwargs from start_observation / create_score.
    """
    observations = []
    scores = []

    class MockObservation:
        def __init__(self, **kw):
            self._kw = kw
            observations.append(kw)

        def start_observation(self, **kw):
            return MockObservation(**kw)

        def end(self):
            pass

    class MockClient:
        def start_observation(self, **kw):
            return MockObservation(**kw)

        def create_score(self, **kw):
            scores.append(kw)

        def flush(self):
            pass

    return MockClient(), observations, scores


class TestLangFuseSinkWithMockClient:
    """LangFuse sink forwards correctly when a mock client is injected."""

    def setup_method(self):
        reset_client()

    def teardown_method(self):
        reset_client()

    def test_forward_creates_trace_with_correct_fields(self, monkeypatch):
        import observability.langfuse_sink as sink

        mock_client, observations, scores = _make_mock_client()
        sink._langfuse_client = mock_client
        sink._initialised = True

        record = TraceRecord(
            trace_id="tid-lf-1",
            event_id="SO-LF-1",
            intent_selected="CONTRACTUAL_CORRECTION",
            skill_name="pricing-reconciliation",
            shadow_verdict="GREEN",
            shadow_policy_hits=[],
            recipe_name="PriceAdjustmentRecipe.py",
            final_status="COMPLETE",
            explanation="ok",
        )
        assert forward(record) is True
        # Root observation is created
        assert observations[0]["name"] == "asoe-graph-execution"
        assert observations[0]["input"] == {"event_id": "SO-LF-1"}

    def test_forward_creates_spans_for_each_pipeline_stage(self, monkeypatch):
        import observability.langfuse_sink as sink

        mock_client, observations, scores = _make_mock_client()
        sink._langfuse_client = mock_client
        sink._initialised = True

        record = TraceRecord(
            trace_id="tid-lf-2",
            event_id="SO-LF-2",
            intent_selected="CREDIT_BLOCK",
            skill_name="credit-hold",
            shadow_verdict="GREEN",
            shadow_policy_hits=[],
            recipe_name="CreditHoldReleaseRecipe.py",
            gateway_calls=[
                "dep:fulfillment_status:resolved",
                "notification/send:SUCCESS",
            ],
            final_status="COMPLETE",
        )
        forward(record)

        obs_names = [o["name"] for o in observations]
        # All 11 graph nodes from architecture_v4.md §5.1 emit a span
        # when their owning signal is present in TraceRecord. A GREEN
        # full run touches every node.
        assert "ingest" in obs_names
        assert "classify" in obs_names
        assert "load_skill" in obs_names
        assert "validate_circuit_breaker" in obs_names
        assert "select_recipe" in obs_names
        assert "resolve_dependencies" in obs_names
        assert "validate_types" in obs_names
        assert "shadow_audit" in obs_names
        assert "execute_recipe" in obs_names
        assert "apply_effects" in obs_names
        assert "build_analysis" in obs_names

    def test_forward_skips_execute_recipe_on_yellow_shadow(self, monkeypatch):
        """YELLOW / RED halt at shadow — execute_recipe never runs."""
        import observability.langfuse_sink as sink

        mock_client, observations, scores = _make_mock_client()
        sink._langfuse_client = mock_client
        sink._initialised = True

        record = TraceRecord(
            trace_id="t", event_id="e",
            intent_selected="CREDIT_BLOCK",
            shadow_verdict="YELLOW",
            shadow_policy_hits=["CREDIT_RELEASE_REVIEW"],
            recipe_name="CreditHoldReleaseRecipe.py",
            final_status="MANUAL_REVIEW_REQUIRED",
        )
        forward(record)

        obs_names = [o["name"] for o in observations]
        assert "shadow_audit" in obs_names
        assert "select_recipe" in obs_names
        assert "execute_recipe" not in obs_names

    def test_forward_classify_carries_backend_used_metadata(self, monkeypatch):
        """classify / select_recipe / shadow_audit metadata records the
        backend tier (provider:model_id) that served each task — so an
        operator can tell at a glance whether a remote LLM ran or the
        deterministic backend served the call."""
        import observability.langfuse_sink as sink
        from contracts.models import LLMCallTrace

        mock_client, observations, scores = _make_mock_client()
        sink._langfuse_client = mock_client
        sink._initialised = True

        record = TraceRecord(
            trace_id="t", event_id="e",
            intent_selected="DUPLICATE_PO",
            shadow_verdict="GREEN",
            recipe_name="DuplicatePORecipe.py",
            final_status="COMPLETE",
            llm_calls=[
                LLMCallTrace(
                    task="intent",
                    provider="anthropic",
                    model_id="claude-sonnet-4-6",
                    input_tokens=400, output_tokens=60,
                ),
            ],
        )
        forward(record)

        classify = next(o for o in observations if o.get("name") == "classify")
        assert classify["metadata"]["backend_used"] == "anthropic:claude-sonnet-4-6"
        # No LLM call for shadow → deterministic
        shadow = next(o for o in observations if o.get("name") == "shadow_audit")
        assert shadow["metadata"]["backend_used"] == "deterministic"

    def test_forward_shadow_audit_span_warning_level_on_non_green(self, monkeypatch):
        import observability.langfuse_sink as sink

        mock_client, observations, scores = _make_mock_client()
        sink._langfuse_client = mock_client
        sink._initialised = True

        record = TraceRecord(
            trace_id="t", event_id="e",
            shadow_verdict="RED",
            shadow_policy_hits=["MASS_ERROR_POLICY"],
        )
        forward(record)

        shadow_obs = [o for o in observations if o.get("name") == "shadow_audit"][0]
        assert shadow_obs["level"] == "WARNING"

    def test_forward_shadow_audit_span_default_level_on_green(self, monkeypatch):
        import observability.langfuse_sink as sink

        mock_client, observations, scores = _make_mock_client()
        sink._langfuse_client = mock_client
        sink._initialised = True

        record = TraceRecord(
            trace_id="t", event_id="e",
            shadow_verdict="GREEN",
            shadow_policy_hits=[],
        )
        forward(record)

        shadow_obs = [o for o in observations if o.get("name") == "shadow_audit"][0]
        assert shadow_obs["level"] == "DEFAULT"

    def test_forward_score_1_on_complete(self, monkeypatch):
        import observability.langfuse_sink as sink

        mock_client, observations, scores = _make_mock_client()
        sink._langfuse_client = mock_client
        sink._initialised = True

        record = TraceRecord(trace_id="t", event_id="e", final_status="COMPLETE")
        forward(record)

        assert len(scores) == 1
        assert scores[0]["value"] == 1.0
        assert scores[0]["comment"] == "COMPLETE"

    def test_forward_score_0_on_fail_to_human(self, monkeypatch):
        import observability.langfuse_sink as sink

        mock_client, observations, scores = _make_mock_client()
        sink._langfuse_client = mock_client
        sink._initialised = True

        record = TraceRecord(trace_id="t", event_id="e", final_status="FAIL_TO_HUMAN")
        forward(record)

        assert len(scores) == 1
        assert scores[0]["value"] == 0.0

    def test_forward_skips_spans_when_no_graph_signal(self, monkeypatch):
        """A handcrafted minimal TraceRecord (no final_status, intent,
        skill, shadow, or recipe) doesn't represent a real graph run, so
        the sink emits only the root observation. Real graph runs always
        carry at least final_status; that path is tested elsewhere."""
        import observability.langfuse_sink as sink

        mock_client, observations, scores = _make_mock_client()
        sink._langfuse_client = mock_client
        sink._initialised = True

        record = TraceRecord(trace_id="t", event_id="e")
        forward(record)

        assert len(observations) == 1
        assert observations[0]["name"] == "asoe-graph-execution"

    def test_forward_emits_generation_per_llm_call(self, monkeypatch):
        """Each LLMCallTrace becomes a 'generation'-typed observation
        with model + token usage + provider metadata. Cost / cache /
        fallback signals carried in metadata for dashboard queries."""
        import observability.langfuse_sink as sink
        from contracts.models import LLMCallTrace

        mock_client, observations, scores = _make_mock_client()
        sink._langfuse_client = mock_client
        sink._initialised = True

        record = TraceRecord(
            trace_id="tid-llm-1",
            event_id="SO-LLM-1",
            intent_selected="DUPLICATE_PO",
            recipe_name="DuplicatePORecipe.py",
            final_status="COMPLETE",
            llm_calls=[
                LLMCallTrace(
                    task="intent",
                    provider="anthropic",
                    model_id="claude-sonnet-4-6",
                    request_id="req_abc",
                    prompt_hash="a" * 64,
                    tool_call_hash="b" * 64,
                    input_tokens=500,
                    output_tokens=80,
                    cache_read_input_tokens=6000,
                    latency_ms=420,
                    cost_usd_estimate=0.005,
                    stop_reason="tool_use",
                ),
                LLMCallTrace(
                    task="recipe",
                    provider="anthropic",
                    model_id="claude-sonnet-4-6",
                    request_id="req_def",
                    input_tokens=200,
                    output_tokens=20,
                ),
            ],
        )
        forward(record)

        # Two generation observations, one per LLMCallTrace
        gens = [o for o in observations if o.get("name", "").startswith("llm.")]
        assert len(gens) == 2
        intent_gen = next(g for g in gens if g["name"] == "llm.intent")
        recipe_gen = next(g for g in gens if g["name"] == "llm.recipe")

        # v4 path tags as_type="generation"
        assert intent_gen.get("as_type") == "generation"
        assert recipe_gen.get("as_type") == "generation"

        # Native LangFuse fields
        assert intent_gen["model"] == "claude-sonnet-4-6"
        assert intent_gen["usage"]["input"] == 500
        assert intent_gen["usage"]["output"] == 80
        assert intent_gen["usage"]["total"] == 580

        # Metadata carries audit fields
        meta = intent_gen["metadata"]
        assert meta["provider"] == "anthropic"
        assert meta["request_id"] == "req_abc"
        assert meta["cache_read_input_tokens"] == 6000
        assert meta["cost_usd_estimate"] == 0.005
        assert meta["fallback_to_deterministic"] is False

        # Default level when no fallback / disagreement
        assert intent_gen["level"] == "DEFAULT"

    def test_forward_generation_warning_level_on_fallback(self, monkeypatch):
        """fallback_to_deterministic → level=WARNING so dashboards
        surface degraded calls."""
        import observability.langfuse_sink as sink
        from contracts.models import LLMCallTrace

        mock_client, observations, scores = _make_mock_client()
        sink._langfuse_client = mock_client
        sink._initialised = True

        record = TraceRecord(
            trace_id="t", event_id="e",
            llm_calls=[
                LLMCallTrace(
                    task="intent",
                    provider="anthropic",
                    model_id="",
                    fallback_to_deterministic=True,
                    fallback_reason="rate_limit",
                ),
            ],
        )
        forward(record)
        gen = next(o for o in observations if o.get("name") == "llm.intent")
        assert gen["level"] == "WARNING"
        assert gen["status_message"] == "rate_limit"
        assert gen["model"] == "(fallback)"

    def test_forward_generation_warning_level_on_disagreement(self, monkeypatch):
        """cross_check_disagreement → level=WARNING + cross-check
        signals carried in metadata."""
        import observability.langfuse_sink as sink
        from contracts.models import LLMCallTrace

        mock_client, observations, scores = _make_mock_client()
        sink._langfuse_client = mock_client
        sink._initialised = True

        record = TraceRecord(
            trace_id="t", event_id="e",
            llm_calls=[
                LLMCallTrace(
                    task="intent",
                    provider="anthropic",
                    model_id="claude-sonnet-4-6",
                    cross_check_disagreement=True,
                    cross_check_llm_intent="DUPLICATE_PO",
                    cross_check_deterministic_intent="CONTRACTUAL_CORRECTION",
                ),
            ],
        )
        forward(record)
        gen = next(o for o in observations if o.get("name") == "llm.intent")
        assert gen["level"] == "WARNING"
        assert gen["output"]["cross_check"] == "DISAGREEMENT"
        assert gen["metadata"]["cross_check_llm_intent"] == "DUPLICATE_PO"
        assert (
            gen["metadata"]["cross_check_deterministic_intent"]
            == "CONTRACTUAL_CORRECTION"
        )

    def test_forward_no_generations_when_no_llm_calls(self, monkeypatch):
        import observability.langfuse_sink as sink

        mock_client, observations, scores = _make_mock_client()
        sink._langfuse_client = mock_client
        sink._initialised = True

        record = TraceRecord(trace_id="t", event_id="e", final_status="COMPLETE")
        forward(record)
        assert not any(o.get("name", "").startswith("llm.") for o in observations)

    def test_forward_v2_emits_generation(self, monkeypatch):
        """v2 LangFuse SDK path: generations are attached as children of
        their owning step span (intent → classify, recipe →
        select_recipe, shadow → shadow_audit). Orphans fall back to
        trace.generation()."""
        import observability.langfuse_sink as sink
        from contracts.models import LLMCallTrace

        spans: list = []
        gens: list = []
        scores: list = []

        class V2Span:
            def generation(self, **kw):
                gens.append(kw)

        class V2Trace:
            def span(self, **kw):
                spans.append(kw)
                return V2Span()

            def generation(self, **kw):
                gens.append(kw)

            def score(self, **kw):
                scores.append(kw)

        class V2Client:
            def trace(self, **kw):
                return V2Trace()

            def flush(self):
                pass

        sink._langfuse_client = V2Client()
        sink._initialised = True

        # task=shadow with no shadow_verdict → orphan, attaches to
        # trace root (legacy v2 path).
        record = TraceRecord(
            trace_id="t-v2", event_id="e",
            llm_calls=[
                LLMCallTrace(
                    task="shadow", provider="ollama", model_id="qwen2.5",
                    input_tokens=100, output_tokens=20,
                ),
            ],
            final_status="COMPLETE",
        )
        forward(record)

        assert len(gens) == 1
        assert gens[0]["name"] == "llm.shadow"
        assert gens[0]["model"] == "qwen2.5"
        # v2 generation does NOT carry as_type — that's a v4 concept
        assert "as_type" not in gens[0]

    def test_forward_v2_attaches_generation_to_owning_span(self, monkeypatch):
        """v2: when intent_selected is set, the classify span emits and
        the intent generation lands as a child of classify (via
        span.generation()), not as a sibling of the trace root."""
        import observability.langfuse_sink as sink
        from contracts.models import LLMCallTrace

        # Track which span each generation was created on.
        span_to_gens: dict[str, list] = {}
        scores: list = []

        class V2Span:
            def __init__(self, name: str):
                self.name = name
                span_to_gens.setdefault(name, [])

            def generation(self, **kw):
                span_to_gens[self.name].append(kw)

        root_gens: list = []

        class V2Trace:
            def span(self, **kw):
                return V2Span(kw["name"])

            def generation(self, **kw):
                root_gens.append(kw)

            def score(self, **kw):
                scores.append(kw)

        class V2Client:
            def trace(self, **kw):
                return V2Trace()

            def flush(self):
                pass

        sink._langfuse_client = V2Client()
        sink._initialised = True

        record = TraceRecord(
            trace_id="t-v2-c", event_id="e",
            intent_selected="DUPLICATE_PO",
            recipe_name="DuplicatePORecipe.py",
            shadow_verdict="GREEN",
            final_status="COMPLETE",
            llm_calls=[
                LLMCallTrace(
                    task="intent", provider="anthropic",
                    model_id="claude-sonnet-4-6",
                    input_tokens=300, output_tokens=40,
                ),
                LLMCallTrace(
                    task="recipe", provider="anthropic",
                    model_id="claude-sonnet-4-6",
                    input_tokens=200, output_tokens=20,
                ),
            ],
        )
        forward(record)

        assert len(span_to_gens["classify"]) == 1
        assert span_to_gens["classify"][0]["name"] == "llm.intent"
        assert len(span_to_gens["select_recipe"]) == 1
        assert span_to_gens["select_recipe"][0]["name"] == "llm.recipe"
        # Nothing on the trace root — both LLM tasks had owners.
        assert root_gens == []

    def test_forward_catches_client_exception(self, monkeypatch):
        import observability.langfuse_sink as sink

        def _exploding_start(**kw):
            raise RuntimeError("LangFuse down")

        mock_client = type("MockClient", (), {
            "start_observation": _exploding_start,
            "create_score": lambda self, **kw: None,
            "flush": lambda self: None,
        })()

        sink._langfuse_client = mock_client
        sink._initialised = True

        record = TraceRecord(trace_id="t", event_id="e", final_status="COMPLETE")
        # Must not raise — returns False
        assert forward(record) is False


class TestTracerEmitWithLangFuse:
    """Tracer.emit() calls LangFuse sink without blocking stdlib logging."""

    def setup_method(self):
        reset_client()

    def teardown_method(self):
        reset_client()

    def test_emit_still_writes_to_stdlib_logger(self, caplog, monkeypatch):
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        record = TraceRecord(trace_id="tid-dual", event_id="SO-D")
        with caplog.at_level(logging.INFO, logger="asoe.observability"):
            Tracer().emit(record)
        payload = json.loads(caplog.records[0].message)
        assert payload["trace"]["trace_id"] == "tid-dual"

    def test_emit_does_not_raise_when_sink_fails(self, caplog, monkeypatch):
        # Patch forward to raise — emit must not propagate
        monkeypatch.setattr(
            "observability.langfuse_sink.forward",
            lambda r: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        record = TraceRecord(trace_id="t", event_id="e")
        with caplog.at_level(logging.INFO, logger="asoe.observability"):
            Tracer().emit(record)  # must not raise
        # stdlib log still emitted
        assert len(caplog.records) >= 1


# ---------------------------------------------------------------------------
# Override audit fields (Phase E)
# ---------------------------------------------------------------------------

class TestOverrideAuditFields:
    """Override audit fields on ExecutionLog and TraceRecord."""

    def test_execution_log_override_fields_default_to_none(self):
        log = ExecutionLog(trace_id="t")
        assert log.resolved_by is None
        assert log.resolved_action is None
        assert log.resolution_notes is None

    def test_execution_log_accepts_override_fields(self):
        log = ExecutionLog(
            trace_id="t",
            resolved_by="analyst@example.com",
            resolved_action="ALLOW_BOTH",
            resolution_notes="Confirmed reorder by buyer.",
        )
        assert log.resolved_by == "analyst@example.com"
        assert log.resolved_action == "ALLOW_BOTH"
        assert log.resolution_notes == "Confirmed reorder by buyer."

    def test_trace_record_override_fields_default_to_none(self):
        record = TraceRecord(trace_id="t", event_id="e")
        assert record.resolved_by is None
        assert record.resolved_action is None
        assert record.resolution_notes is None

    def test_trace_record_accepts_override_fields(self):
        record = TraceRecord(
            trace_id="t", event_id="e",
            resolved_by="manager@example.com",
            resolved_action="SUPERSEDE",
            resolution_notes="Revised PO confirmed by buyer.",
        )
        assert record.resolved_by == "manager@example.com"
        assert record.resolved_action == "SUPERSEDE"

    def test_build_record_extracts_override_from_execution_log(self):
        state = _minimal_state()
        shadow = _green_shadow()
        state.shadow = shadow
        state.execution_log = ExecutionLog(
            trace_id=shadow.trace_id,
            recipe_name="DuplicatePORecipe.py",
            resolved_by="ops@example.com",
            resolved_action="MERGE",
            resolution_notes="Buyer confirmed amendment.",
        )
        state.final_status = TerminalStatus.COMPLETE
        record = Tracer().build_record(state)
        assert record.resolved_by == "ops@example.com"
        assert record.resolved_action == "MERGE"
        assert record.resolution_notes == "Buyer confirmed amendment."

    def test_build_record_override_none_when_no_execution_log(self):
        state = _minimal_state()
        record = Tracer().build_record(state)
        assert record.resolved_by is None
        assert record.resolved_action is None
        assert record.resolution_notes is None

    def test_override_fields_in_json_output(self):
        record = TraceRecord(
            trace_id="t", event_id="e",
            resolved_by="user1", resolved_action="BLOCK_AND_NOTIFY",
            resolution_notes="True duplicate confirmed.",
        )
        data = json.loads(record.to_json())
        assert data["resolved_by"] == "user1"
        assert data["resolved_action"] == "BLOCK_AND_NOTIFY"
        assert data["resolution_notes"] == "True duplicate confirmed."
