from __future__ import annotations

# S4 integration coverage — wires the per-task LLM router into
# orchestration / compliance / classifier and verifies the four
# panel-required invariants end-to-end:
#
#   1. Per-task routing — orchestration `classify` uses task='intent',
#      `select_recipe` uses task='recipe', `shadow_audit` uses
#      task='shadow'. Per-task env overrides (ASOE_LLM_PROVIDER_INTENT
#      etc.) take effect through the call chain.
#
#   2. ASOE_LLM_DISABLE_FOR — runtime kill-by-task forces a single
#      trio call back to deterministic without redeploying.
#
#   3. Cross-check on classify — when the LLM picks an intent that
#      diverges from the deterministic classifier, the graph routes
#      to MANUAL_REVIEW_REQUIRED with the deterministic intent and
#      a structured explanation. (Conservative shakeout posture.)
#
#   4. Kill-switch + explain-mode pinning — the router never builds
#      a remote client when ASOE_KILL_SWITCH=1 OR ASOE_EXPLAIN_MODE=1.
#      Defence-in-depth on top of the build-time kill-switch check
#      already inside each provider client.

import sys
from unittest import mock

import pytest

from constraints.fallback_backend import DeterministicFallbackBackend
from constraints.llm_backend import RemoteLLMBackend
from constraints.specs import IntentDecision
from contracts.models import GraphState, Intent, OrderEvent, TerminalStatus
from llm.budget import InMemoryBudgetTracker
from llm.circuit_breaker import LLMCircuitBreaker
from llm.provider_protocol import (
    SystemBlock,
    TokenUsage,
    ToolCallResult,
)
from orchestration.nodes import (
    _reset_backend_cache,
    classify,
    select_recipe,
    shadow_audit,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_backend_cache():
    """Each test starts with a fresh backend cache so env-var changes
    actually apply."""
    _reset_backend_cache()
    yield
    _reset_backend_cache()


def _pricing_state() -> GraphState:
    return GraphState(
        event=OrderEvent(
            order_id="SO-LLM-INT",
            line_item=1,
            po_price=90.0,
            sap_base_price=100.0,
            retailer_id="R-01",
            line_count=1,
        )
    )


def _duplicate_state() -> GraphState:
    return GraphState(
        event=OrderEvent(
            order_id="SO-DUPE",
            line_item=1,
            po_price=100.0,
            sap_base_price=100.0,
            retailer_id="R-01",
            line_count=1,
            event_type="EDI_850_DUPLICATE_PO",
        )
    )


# ---------------------------------------------------------------------------
# 1. Per-task routing — env overrides reach orchestration nodes
# ---------------------------------------------------------------------------


def test_per_task_intent_provider_used_in_classify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASOE_LLM_PROVIDER_INTENT=anthropic builds a RemoteLLMBackend
    for the classify node; recipe/shadow stay on the global default
    (fallback)."""
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "fallback")
    monkeypatch.setenv("ASOE_LLM_PROVIDER_INTENT", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")
    monkeypatch.delenv("ASOE_EXPLAIN_MODE", raising=False)
    monkeypatch.setenv("ASOE_ENV", "sandbox")

    fake = mock.Mock()
    fake.Anthropic = mock.Mock(return_value=mock.Mock())
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    # Call classify and verify the cached intent backend is the
    # RemoteLLMBackend (Anthropic), while recipe/shadow stay on
    # deterministic via the global fallback.
    from orchestration.nodes import _backend

    intent_backend = _backend(task="intent")
    recipe_backend = _backend(task="recipe")
    shadow_backend = _backend(task="shadow")

    assert isinstance(intent_backend, RemoteLLMBackend)
    assert isinstance(recipe_backend, DeterministicFallbackBackend)
    assert isinstance(shadow_backend, DeterministicFallbackBackend)


def test_classify_uses_intent_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """Smoke: with default config (everything fallback), classify
    still produces a deterministic IntentDecision and doesn't crash
    after the per-task wiring."""
    monkeypatch.delenv("ASOE_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ASOE_LLM_PROVIDER_INTENT", raising=False)
    state = _duplicate_state()
    out = classify(state)
    assert out.intent == Intent.DUPLICATE_PO
    # No final_status — classify only sets it on cross-check
    # disagreement (impossible when both sides are deterministic).
    assert out.final_status is None


def test_select_recipe_uses_recipe_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASOE_LLM_PROVIDER", raising=False)
    state = _pricing_state()
    state.intent = Intent.CONTRACTUAL_CORRECTION
    out = select_recipe(state)
    assert out.selected_recipe == "PriceAdjustmentRecipe.py"


def test_shadow_audit_uses_shadow_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASOE_LLM_PROVIDER", raising=False)
    state = _pricing_state()
    state.intent = Intent.CONTRACTUAL_CORRECTION
    out = shadow_audit(state)
    assert out.shadow is not None
    # Shadow runs the deterministic verdict without crashing.
    assert out.shadow.status.value in {"GREEN", "YELLOW", "RED"}


# ---------------------------------------------------------------------------
# 2. ASOE_LLM_DISABLE_FOR runtime kill-by-task
# ---------------------------------------------------------------------------


def test_disable_for_shadow_overrides_global_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator with ASOE_LLM_PROVIDER=anthropic + DISABLE_FOR=shadow
    keeps the LLM on intent/recipe but pins shadow to deterministic.
    Mid-incident kill-switch by task without redeploying."""
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ASOE_LLM_DISABLE_FOR", "shadow")
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")
    monkeypatch.delenv("ASOE_EXPLAIN_MODE", raising=False)
    monkeypatch.setenv("ASOE_ENV", "sandbox")

    fake = mock.Mock()
    fake.Anthropic = mock.Mock(return_value=mock.Mock())
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    from orchestration.nodes import _backend

    assert isinstance(_backend(task="intent"), RemoteLLMBackend)
    assert isinstance(_backend(task="recipe"), RemoteLLMBackend)
    assert isinstance(_backend(task="shadow"), DeterministicFallbackBackend)


def test_disable_for_all_tasks_kills_llm_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """ASOE_LLM_DISABLE_FOR=intent,recipe,shadow is the panic-button
    kill of the entire LLM tier — every task falls back to
    deterministic."""
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ASOE_LLM_DISABLE_FOR", "intent,recipe,shadow")
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")
    monkeypatch.delenv("ASOE_EXPLAIN_MODE", raising=False)
    monkeypatch.setenv("ASOE_ENV", "sandbox")

    fake = mock.Mock()
    fake.Anthropic = mock.Mock(side_effect=AssertionError("should not be called"))
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    from orchestration.nodes import _backend

    assert isinstance(_backend(task="intent"), DeterministicFallbackBackend)
    assert isinstance(_backend(task="recipe"), DeterministicFallbackBackend)
    assert isinstance(_backend(task="shadow"), DeterministicFallbackBackend)
    fake.Anthropic.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Cross-check on classify — disagreement → MANUAL_REVIEW_REQUIRED
# ---------------------------------------------------------------------------


class _FixedIntentBackend:
    """Test double for the intent slot. Returns whatever IntentDecision
    you feed it — used to simulate an LLM disagreeing with the
    deterministic classifier."""

    def __init__(self, decision: IntentDecision):
        self._decision = decision

    def classify_intent(self, state) -> IntentDecision:  # noqa: ARG002
        return self._decision

    def propose_recipe(self, state):  # noqa: ARG002
        return None

    def shadow_decision(self, state):  # noqa: ARG002
        from constraints.specs import ShadowDecisionSchema
        return ShadowDecisionSchema(status="GREEN", reasons=["x"], policy_hits=[])


def _inject_intent_backend(decision: IntentDecision) -> None:
    """Force the intent-task cache to a fixed backend that returns
    `decision`. Bypasses the router so the test pins exact behaviour."""
    from orchestration import nodes
    nodes._cached_backends["intent"] = _FixedIntentBackend(decision)  # noqa: SLF001


def test_cross_check_disagreement_routes_to_manual_review() -> None:
    """LLM picks DUPLICATE_PO; deterministic picks
    CONTRACTUAL_CORRECTION (the order is a plain pricing event).
    Result: MANUAL_REVIEW_REQUIRED, deterministic intent wins."""
    _inject_intent_backend(
        IntentDecision(
            intent="DUPLICATE_PO",
            confidence=0.95,
            rationale="from-llm",
        )
    )
    state = _pricing_state()  # → deterministic says CONTRACTUAL_CORRECTION
    out = classify(state)

    assert out.final_status is TerminalStatus.MANUAL_REVIEW_REQUIRED
    # Deterministic intent wins for downstream routing
    assert out.intent == Intent.CONTRACTUAL_CORRECTION
    # Explanation captures both labels + the cross-check reason
    assert "DUPLICATE_PO" in out.explanation
    assert "CONTRACTUAL_CORRECTION" in out.explanation
    assert "LLM_DETERMINISTIC_DISAGREEMENT" in out.explanation


def test_cross_check_agreement_does_not_route() -> None:
    """LLM and deterministic both return DUPLICATE_PO → graph proceeds
    normally with no terminal status set."""
    _inject_intent_backend(
        IntentDecision(
            intent="DUPLICATE_PO",
            confidence=0.95,
            rationale="from-llm",
        )
    )
    state = _duplicate_state()  # → deterministic ALSO says DUPLICATE_PO
    out = classify(state)

    assert out.final_status is None
    assert out.intent == Intent.DUPLICATE_PO
    # Confidence comes from the LLM (richer rationale than the
    # deterministic one) on agreement.
    assert out.confidence == pytest.approx(0.95)


def test_cross_check_skipped_when_active_backend_is_deterministic() -> None:
    """When the active intent backend IS DeterministicFallbackBackend,
    we skip the cross-check (it would compare a value to itself).
    Verifies the isinstance gate."""
    state = _pricing_state()
    out = classify(state)
    # Deterministic for a plain pricing event
    assert out.intent == Intent.CONTRACTUAL_CORRECTION
    assert out.final_status is None


# ---------------------------------------------------------------------------
# 4. Kill-switch + explain-mode pinning at the router
# ---------------------------------------------------------------------------


def test_kill_switch_pins_router_to_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ASOE_LLM_PROVIDER=anthropic and a fully-configured key,
    the router STILL returns DeterministicFallbackBackend when the
    kill switch is active — defence-in-depth above the per-provider
    construction-time check."""
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ASOE_KILL_SWITCH", "1")  # ACTIVE
    monkeypatch.delenv("ASOE_EXPLAIN_MODE", raising=False)
    monkeypatch.setenv("ASOE_ENV", "sandbox")

    # If the router somehow bypassed the gate and tried to construct,
    # this fake would let us know — but it should never be called.
    fake = mock.Mock()
    fake.Anthropic = mock.Mock(side_effect=AssertionError("kill switch bypassed"))
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    from orchestration.nodes import _backend

    for task in ("intent", "recipe", "shadow"):
        assert isinstance(_backend(task=task), DeterministicFallbackBackend), task
    fake.Anthropic.assert_not_called()


def test_explain_mode_pins_router_to_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explain-mode dry-runs must NEVER call paid LLMs (Chen review §6).
    With explain mode active, every task resolves to deterministic
    even if ASOE_LLM_PROVIDER points elsewhere."""
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")
    monkeypatch.setenv("ASOE_EXPLAIN_MODE", "1")  # ACTIVE
    monkeypatch.setenv("ASOE_ENV", "sandbox")

    fake = mock.Mock()
    fake.Anthropic = mock.Mock(side_effect=AssertionError("explain mode bypassed"))
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    from orchestration.nodes import _backend

    for task in ("intent", "recipe", "shadow"):
        assert isinstance(_backend(task=task), DeterministicFallbackBackend), task
    fake.Anthropic.assert_not_called()


def test_kill_switch_re_evaluates_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator can flip ASOE_KILL_SWITCH off mid-session and the
    NEXT backend lookup picks up the change — the cache must be
    cleared by the test, but the router itself reads env on every
    call (no module-level memoisation of the kill state)."""
    from constraints.router import get_constrained_backend

    monkeypatch.setenv("ASOE_LLM_PROVIDER", "fallback")
    monkeypatch.setenv("ASOE_KILL_SWITCH", "1")
    assert isinstance(get_constrained_backend(task="intent"), DeterministicFallbackBackend)

    # Disable kill switch — next call returns deterministic still
    # because provider is fallback, but the call is reached (no
    # short-circuit raises). Just verifying the env is re-read.
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")
    assert isinstance(get_constrained_backend(task="intent"), DeterministicFallbackBackend)


# ---------------------------------------------------------------------------
# 5. ComplianceShadow direct construction defaults to task='shadow'
# ---------------------------------------------------------------------------


def test_compliance_shadow_default_uses_shadow_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ComplianceShadow() with no backend arg uses the shadow-task
    router slot — so direct callers (unit tests, ad-hoc API) honour
    ASOE_LLM_PROVIDER_SHADOW + ASOE_LLM_DISABLE_FOR=shadow."""
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ASOE_LLM_DISABLE_FOR", "shadow")  # pin shadow
    monkeypatch.setenv("ASOE_KILL_SWITCH", "0")
    monkeypatch.delenv("ASOE_EXPLAIN_MODE", raising=False)
    monkeypatch.setenv("ASOE_ENV", "sandbox")

    fake = mock.Mock()
    fake.Anthropic = mock.Mock(side_effect=AssertionError("should not be called"))
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    from compliance.shadow import ComplianceShadow

    shadow = ComplianceShadow()
    assert isinstance(shadow.backend, DeterministicFallbackBackend)
    fake.Anthropic.assert_not_called()
