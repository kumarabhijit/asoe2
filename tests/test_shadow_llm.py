"""ADR-039 Phase X.1 — `compliance/shadow_llm.py` primitive tests.

Coverage targets the constrained surface that must hold for Phase
X.1 (observe-only) to be Compliance-defensible:

  * Bundle loading — system prompt, vocabulary, rollout config.
  * Gating per ADR-039 §5.2 — the three trigger paths and the
    short-circuit on deterministic-RED.
  * Cache discipline per ADR-039 §5.5 — per-tenant key,
    24h TTL, cache_hit annotation.
  * SLI counters per ADR-039 §7.3 — invocations, verdicts by
    action, cache hits, skip counters.
  * Asymmetric authority enforcement — schema rejects
    `DISAGREE_UPGRADE`; out-of-vocab concerns dropped.
  * Provider failure modes — timeout, validation, generic
    unavailability all fall through to a None verdict.

Tests use the deterministic stub provider so the suite never
hits the network. The harness wire-up is Thread 4; this suite
covers only the primitive.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

import pytest
from pydantic import ValidationError

from compliance.shadow_llm import (
    SHADOW_LLM_BUNDLE_DIR,
    SKIP_BELOW_FLOOR,
    SKIP_DETERMINISTIC_RED,
    SKIP_PROVIDER_TIMEOUT,
    SKIP_PROVIDER_UNAVAILABLE,
    SKIP_VALIDATION_ERROR,
    TRIGGER_DETERMINISTIC_YELLOW,
    TRIGGER_FINANCIAL_IMPACT,
    LLMShadowProvider,
    ShadowLLM,
    ShadowLLMBundle,
    ShadowLLMCache,
    ShadowLLMMetrics,
    ShadowLLMRequest,
    StubLLMShadowProvider,
    load_bundle,
    shadow_llm_cache,
    shadow_llm_metrics,
)
from contracts.models import ComplianceDecision, ShadowLLMVerdict, ShadowStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_module_state():
    shadow_llm_cache.clear()
    shadow_llm_metrics.reset()
    yield
    shadow_llm_cache.clear()
    shadow_llm_metrics.reset()


@pytest.fixture
def bundle() -> ShadowLLMBundle:
    return load_bundle()


@pytest.fixture
def request_factory():
    def _make(**overrides: Any) -> ShadowLLMRequest:
        defaults: dict[str, Any] = {
            "intent": "DUPLICATE_PO",
            "recipe_name": "DuplicatePORecipe",
            "recipe_params": {"po_number": "PO-1"},
            "proposed_action": "BLOCK_DUPLICATE",
            "deterministic_status": "GREEN",
            "deterministic_reasons": (),
            "deterministic_policy_hits": (),
            "case_context_summary": None,
            "customer_profile": {"tier": "Strategic"},
        }
        defaults.update(overrides)
        return ShadowLLMRequest(**defaults)
    return _make


@pytest.fixture
def shadow(bundle):
    return ShadowLLM(provider=StubLLMShadowProvider(), bundle=bundle)


def _decision(status: ShadowStatus, *reasons: str) -> ComplianceDecision:
    return ComplianceDecision(status=status, reasons=list(reasons))


# ---------------------------------------------------------------------------
# Schema invariants — ADR-039 §3.2 / §4
# ---------------------------------------------------------------------------

class TestVerdictSchema:
    def test_disagree_upgrade_rejected_by_schema(self):
        with pytest.raises(ValidationError):
            ShadowLLMVerdict(
                action="DISAGREE_UPGRADE",  # type: ignore[arg-type]
                reason="should not be possible",
                confidence=0.9,
            )

    def test_action_enum_closed(self):
        for valid in ("AGREE", "DISAGREE_DOWNGRADE", "ABSTAIN"):
            v = ShadowLLMVerdict(action=valid, reason="x", confidence=0.5)  # type: ignore[arg-type]
            assert v.action == valid
        with pytest.raises(ValidationError):
            ShadowLLMVerdict(action="UNKNOWN", reason="x", confidence=0.5)  # type: ignore[arg-type]

    def test_reason_length_capped(self):
        with pytest.raises(ValidationError):
            ShadowLLMVerdict(action="AGREE", reason="x" * 201, confidence=0.5)

    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            ShadowLLMVerdict(action="AGREE", reason="x", confidence=1.5)
        with pytest.raises(ValidationError):
            ShadowLLMVerdict(action="AGREE", reason="x", confidence=-0.1)


# ---------------------------------------------------------------------------
# Bundle loading
# ---------------------------------------------------------------------------

class TestBundleLoading:
    def test_load_bundle_reads_metadata(self, bundle):
        assert bundle.bundle_version == "1.0.0"
        assert bundle.rollout_phase == "X.1"
        # X.1 ships observe-only — threshold is null.
        assert bundle.financial_impact_threshold_usd is None
        assert bundle.invocation_financial_floor_usd == 500.0
        assert bundle.cache_ttl_seconds == 86_400
        assert bundle.inference_temperature == 0.0
        assert bundle.wall_clock_timeout_ms == 2000

    def test_load_bundle_loads_concerns_vocabulary(self, bundle):
        assert len(bundle.concerns_vocabulary) >= 5
        assert "CUSTOMER_OPT_OUT_VIOLATION" in bundle.concerns_vocabulary

    def test_load_bundle_includes_system_prompt(self, bundle):
        assert "L2 LLM Shadow" in bundle.system_prompt
        assert "AGREE" in bundle.system_prompt
        assert "DISAGREE_DOWNGRADE" in bundle.system_prompt
        assert "ABSTAIN" in bundle.system_prompt

    def test_missing_bundle_dir_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_bundle(tmp_path / "does-not-exist")


# ---------------------------------------------------------------------------
# Gating — ADR-039 §5.2
# ---------------------------------------------------------------------------

class TestGating:
    def test_red_short_circuits(self, shadow):
        ok, token = shadow.should_invoke(
            deterministic=_decision(ShadowStatus.RED), financial_impact_usd=1_000_000,
        )
        assert ok is False
        assert token == SKIP_DETERMINISTIC_RED

    def test_yellow_invokes_regardless_of_amount(self, shadow):
        ok, token = shadow.should_invoke(
            deterministic=_decision(ShadowStatus.YELLOW), financial_impact_usd=1.0,
        )
        assert ok is True
        assert token == TRIGGER_DETERMINISTIC_YELLOW

    def test_green_at_floor_invokes(self, shadow):
        ok, token = shadow.should_invoke(
            deterministic=_decision(ShadowStatus.GREEN), financial_impact_usd=500.0,
        )
        assert ok is True
        assert token == TRIGGER_FINANCIAL_IMPACT

    def test_green_below_floor_skipped(self, shadow):
        ok, token = shadow.should_invoke(
            deterministic=_decision(ShadowStatus.GREEN), financial_impact_usd=499.99,
        )
        assert ok is False
        assert token == SKIP_BELOW_FLOOR


# ---------------------------------------------------------------------------
# Stub provider
# ---------------------------------------------------------------------------

class TestStubProvider:
    def test_stub_yellow_returns_agree(self, shadow, request_factory):
        request = request_factory(
            deterministic_status="YELLOW",
            deterministic_reasons=("rule X tripped",),
        )
        outcome = shadow.evaluate(
            tenant_id="t1", request=request,
            deterministic=_decision(ShadowStatus.YELLOW, "rule X tripped"),
        )
        assert outcome.verdict is not None
        assert outcome.verdict.action == "AGREE"
        assert "rule X tripped" in outcome.verdict.reason

    def test_stub_force_disagree_token(self, shadow, request_factory):
        request = request_factory(
            recipe_params={"po_number": "PO-1", "force_disagree": True},
        )
        outcome = shadow.evaluate(
            tenant_id="t1", request=request,
            deterministic=_decision(ShadowStatus.GREEN),
            financial_impact_usd=1_000.0,
        )
        assert outcome.verdict is not None
        assert outcome.verdict.action == "DISAGREE_DOWNGRADE"
        assert outcome.verdict.policy_concerns  # named, in vocab

    def test_stub_default_abstain(self, shadow, request_factory):
        outcome = shadow.evaluate(
            tenant_id="t1", request=request_factory(),
            deterministic=_decision(ShadowStatus.GREEN),
            financial_impact_usd=1_000.0,
        )
        assert outcome.verdict is not None
        assert outcome.verdict.action == "ABSTAIN"


# ---------------------------------------------------------------------------
# Cache — ADR-039 §5.5
# ---------------------------------------------------------------------------

class TestCache:
    def test_second_call_serves_from_cache(self, shadow, request_factory):
        req = request_factory(recipe_params={"po_number": "PO-X"})
        first = shadow.evaluate(
            tenant_id="t1", request=req,
            deterministic=_decision(ShadowStatus.GREEN),
            financial_impact_usd=1_000.0,
        )
        second = shadow.evaluate(
            tenant_id="t1", request=req,
            deterministic=_decision(ShadowStatus.GREEN),
            financial_impact_usd=1_000.0,
        )
        assert first.cache_hit is False
        assert second.cache_hit is True
        assert second.verdict is not None
        assert second.verdict.cache_hit is True
        # Same canonical content.
        assert first.verdict.action == second.verdict.action

    def test_tenant_isolation(self, shadow, request_factory):
        req = request_factory(recipe_params={"po_number": "PO-Y"})
        outcome_a = shadow.evaluate(
            tenant_id="tenant-a", request=req,
            deterministic=_decision(ShadowStatus.GREEN),
            financial_impact_usd=1_000.0,
        )
        outcome_b = shadow.evaluate(
            tenant_id="tenant-b", request=req,
            deterministic=_decision(ShadowStatus.GREEN),
            financial_impact_usd=1_000.0,
        )
        # Both miss cache (different tenants → different keys).
        assert outcome_a.cache_hit is False
        assert outcome_b.cache_hit is False

    def test_ttl_expiry(self, bundle, request_factory):
        cache = ShadowLLMCache()
        req = request_factory()
        key = ShadowLLMCache.make_key(
            tenant_id="t1",
            bundle_version=bundle.bundle_version,
            model_id="stub-llm-shadow-v1",
            request=req,
        )
        v = ShadowLLMVerdict(action="ABSTAIN", reason="x", confidence=0.5)
        # Stamp a cache entry with a now value, then read at now+ttl+1.
        cache.put(key, v, ttl_seconds=10, now=1000.0)
        assert cache.get(key, now=1005.0) is not None
        assert cache.get(key, now=1011.0) is None


# ---------------------------------------------------------------------------
# Out-of-vocab concerns — schema-side defence
# ---------------------------------------------------------------------------

class _OOVProvider:
    model_id = "oov-test"

    def evaluate(self, request, *, bundle, timeout_ms):
        return ShadowLLMVerdict(
            action="DISAGREE_DOWNGRADE",
            reason="oov reason",
            confidence=0.8,
            policy_concerns=["NOT_IN_VOCAB", "CUSTOMER_OPT_OUT_VIOLATION"],
        )


class TestVocabularyValidation:
    def test_out_of_vocab_concerns_dropped(self, bundle, request_factory):
        s = ShadowLLM(provider=_OOVProvider(), bundle=bundle)
        outcome = s.evaluate(
            tenant_id="t1", request=request_factory(),
            deterministic=_decision(ShadowStatus.GREEN),
            financial_impact_usd=1_000.0,
        )
        assert outcome.verdict is not None
        assert outcome.verdict.policy_concerns == ["CUSTOMER_OPT_OUT_VIOLATION"]
        assert s.metrics.validation_errors_total == 1


# ---------------------------------------------------------------------------
# Provider failure modes
# ---------------------------------------------------------------------------

class _TimeoutProvider:
    model_id = "timeout-test"

    def evaluate(self, request, *, bundle, timeout_ms):
        raise TimeoutError("slow upstream")


class _UnavailableProvider:
    model_id = "unavailable-test"

    def evaluate(self, request, *, bundle, timeout_ms):
        raise RuntimeError("provider 5xx")


class _SchemaErrorProvider:
    model_id = "schema-test"

    def evaluate(self, request, *, bundle, timeout_ms):
        raise ValueError("constrained_generation_rejected")


class TestProviderFailureModes:
    def test_timeout_falls_through(self, bundle, request_factory):
        s = ShadowLLM(provider=_TimeoutProvider(), bundle=bundle)
        outcome = s.evaluate(
            tenant_id="t1", request=request_factory(),
            deterministic=_decision(ShadowStatus.GREEN),
            financial_impact_usd=1_000.0,
        )
        assert outcome.verdict is None
        assert outcome.skip_reason == SKIP_PROVIDER_TIMEOUT
        assert s.metrics.timeouts_total == 1

    def test_unavailable_falls_through(self, bundle, request_factory):
        s = ShadowLLM(provider=_UnavailableProvider(), bundle=bundle)
        outcome = s.evaluate(
            tenant_id="t1", request=request_factory(),
            deterministic=_decision(ShadowStatus.GREEN),
            financial_impact_usd=1_000.0,
        )
        assert outcome.verdict is None
        assert outcome.skip_reason == SKIP_PROVIDER_UNAVAILABLE
        assert s.metrics.unavailable_total == 1

    def test_validation_error_falls_through(self, bundle, request_factory):
        s = ShadowLLM(provider=_SchemaErrorProvider(), bundle=bundle)
        outcome = s.evaluate(
            tenant_id="t1", request=request_factory(),
            deterministic=_decision(ShadowStatus.GREEN),
            financial_impact_usd=1_000.0,
        )
        assert outcome.verdict is None
        assert outcome.skip_reason == SKIP_VALIDATION_ERROR
        assert s.metrics.validation_errors_total == 1


# ---------------------------------------------------------------------------
# SLI counters — ADR-039 §7.3
# ---------------------------------------------------------------------------

class TestMetrics:
    def test_skip_red_increments_counter(self, shadow, request_factory):
        outcome = shadow.evaluate(
            tenant_id="t1", request=request_factory(),
            deterministic=_decision(ShadowStatus.RED),
            financial_impact_usd=1_000.0,
        )
        assert outcome.verdict is None
        assert shadow.metrics.skipped_red_total == 1
        assert shadow.metrics.invocations_total == 0

    def test_skip_below_floor_increments_counter(self, shadow, request_factory):
        outcome = shadow.evaluate(
            tenant_id="t1", request=request_factory(),
            deterministic=_decision(ShadowStatus.GREEN),
            financial_impact_usd=10.0,
        )
        assert outcome.verdict is None
        assert shadow.metrics.skipped_below_floor_total == 1
        assert shadow.metrics.invocations_total == 0

    def test_invocation_counts_by_trigger(self, shadow, request_factory):
        # Yellow trigger.
        shadow.evaluate(
            tenant_id="t1", request=request_factory(deterministic_status="YELLOW"),
            deterministic=_decision(ShadowStatus.YELLOW, "x"),
        )
        # Financial-impact trigger.
        shadow.evaluate(
            tenant_id="t1", request=request_factory(),
            deterministic=_decision(ShadowStatus.GREEN),
            financial_impact_usd=1_000.0,
        )
        assert shadow.metrics.invocations_total == 2
        assert shadow.metrics.invocations_by_trigger[TRIGGER_DETERMINISTIC_YELLOW] == 1
        assert shadow.metrics.invocations_by_trigger[TRIGGER_FINANCIAL_IMPACT] == 1

    def test_disagreement_rate(self, shadow, request_factory):
        shadow.evaluate(
            tenant_id="t1",
            request=request_factory(recipe_params={"force_disagree": True}),
            deterministic=_decision(ShadowStatus.GREEN),
            financial_impact_usd=1_000.0,
        )
        shadow.evaluate(
            tenant_id="t1", request=request_factory(),
            deterministic=_decision(ShadowStatus.GREEN),
            financial_impact_usd=1_000.0,
        )
        # 1 DISAGREE_DOWNGRADE + 1 ABSTAIN; rate = 0.5.
        assert shadow.metrics.disagreement_rate() == 0.5
        assert shadow.metrics.abstain_rate() == 0.5

    def test_cache_hit_counter(self, shadow, request_factory):
        req = request_factory()
        shadow.evaluate(
            tenant_id="t1", request=req,
            deterministic=_decision(ShadowStatus.GREEN),
            financial_impact_usd=1_000.0,
        )
        shadow.evaluate(
            tenant_id="t1", request=req,
            deterministic=_decision(ShadowStatus.GREEN),
            financial_impact_usd=1_000.0,
        )
        assert shadow.metrics.invocations_total == 2
        assert shadow.metrics.cache_hits_total == 1
        assert shadow.metrics.cache_hit_rate() == 0.5
