"""ADR-039 X.1 — `shadow_audit` L2 wire-up tests.

Locks the observe-only invariants:
  * Deterministic-RED short-circuits L2 (verdict not stamped).
  * Deterministic-YELLOW always invokes L2; verdict stamped on
    `state.shadow.llm_shadow_verdict`.
  * Deterministic-GREEN below floor → no L2 invocation.
  * Deterministic-GREEN at/above floor → L2 invoked.
  * `LLMCallTrace` task='shadow_llm' appended on every successful
    invocation.
  * `ASOE_SHADOW_LLM_DISABLED=1` kill-switch suppresses the L2 path
    cleanly.
  * X.1 invariant: L2 verdict NEVER changes `state.shadow.status`
    or `state.final_status` from what the deterministic gate produced.
"""

from __future__ import annotations

import pytest

from compliance.shadow_llm import shadow_llm_cache, shadow_llm_metrics
from contracts.models import (
    GraphState,
    Intent,
    OrderEvent,
    ShadowStatus,
    TerminalStatus,
)
from orchestration.nodes import (
    _reset_l2_shadow_cache,
    shadow_audit,
)


@pytest.fixture(autouse=True)
def _reset_state():
    shadow_llm_cache.clear()
    shadow_llm_metrics.reset()
    _reset_l2_shadow_cache()
    yield
    shadow_llm_cache.clear()
    shadow_llm_metrics.reset()
    _reset_l2_shadow_cache()


def _state(
    *,
    intent: Intent,
    po_price: float,
    sap_base_price: float,
    line_count: int = 1,
    requester_role: str | None = None,
    credit_limit: float | None = None,
    current_exposure: float | None = None,
    tenant_id: str | None = "tenant-a",
) -> GraphState:
    """Build a GraphState with `batch_total_variance` pre-set so the
    L2 invocation sees a meaningful financial-impact estimate (the
    real graph stamps it at ingest; tests skip ingest)."""
    state = GraphState(
        event=OrderEvent(
            order_id=f"SO-{intent.value}-1",
            po_price=po_price,
            sap_base_price=sap_base_price,
            line_count=line_count,
            requester_role=requester_role,
            credit_limit=credit_limit,
            current_exposure=current_exposure,
        ),
        tenant_id=tenant_id,
    )
    state.intent = intent
    state.batch_total_variance = abs((po_price - sap_base_price) * line_count)
    return state


# ---------------------------------------------------------------------------
# Wire-up invariants
# ---------------------------------------------------------------------------


class TestL2Invocation:
    def test_red_does_not_invoke_l2(self):
        # MASS_PRICING_ERROR triggers RED via the deterministic backend.
        state = _state(
            intent=Intent.MASS_PRICING_ERROR,
            po_price=70.0, sap_base_price=100.0, line_count=11,
        )
        result = shadow_audit(state)
        assert result.final_status == TerminalStatus.BLOCKED
        assert result.shadow.status == ShadowStatus.RED
        # Observe-only invariant: L2 verdict is None on RED.
        assert result.shadow.llm_shadow_verdict is None

    def test_yellow_invokes_l2(self):
        # CREDIT_BLOCK triggers YELLOW via the deterministic backend.
        state = _state(
            intent=Intent.CREDIT_BLOCK,
            po_price=100.0, sap_base_price=100.0,
            requester_role="ORDER_MANAGER",
            credit_limit=10_000.0, current_exposure=10_100.0,
        )
        result = shadow_audit(state)
        assert result.shadow.status == ShadowStatus.YELLOW
        assert result.shadow.llm_shadow_verdict is not None
        # Stub provider returns AGREE on YELLOW (echoes the
        # deterministic reason).
        assert result.shadow.llm_shadow_verdict.action == "AGREE"

    def test_green_below_floor_skips_l2(self):
        # 1 line × $10 gap = $10 — well below the $500 floor.
        state = _state(
            intent=Intent.CONTRACTUAL_CORRECTION,
            po_price=90.0, sap_base_price=100.0, line_count=1,
        )
        result = shadow_audit(state)
        assert result.shadow.status == ShadowStatus.GREEN
        assert result.shadow.llm_shadow_verdict is None

    def test_green_at_floor_invokes_l2(self):
        # 1 line × $500 gap = $500 — exactly at the floor.
        state = _state(
            intent=Intent.CONTRACTUAL_CORRECTION,
            po_price=500.0, sap_base_price=1000.0, line_count=1,
        )
        result = shadow_audit(state)
        assert result.shadow.status == ShadowStatus.GREEN
        assert result.shadow.llm_shadow_verdict is not None
        assert result.shadow.llm_shadow_verdict.action == "ABSTAIN"

    def test_observe_only_status_unchanged(self):
        """X.1 invariant — even when L2 fires, deterministic
        status drives final routing. The L2 verdict is captured
        but the verdict's `action` does not flip GREEN ↔ YELLOW."""
        state = _state(
            intent=Intent.CONTRACTUAL_CORRECTION,
            po_price=500.0, sap_base_price=1000.0, line_count=1,
        )
        result = shadow_audit(state)
        # Deterministic GREEN proceeds (no final_status terminal).
        assert result.final_status is None
        assert result.shadow.status == ShadowStatus.GREEN
        # L2 stamped its verdict but did not move the gate.
        assert result.shadow.llm_shadow_verdict is not None

    def test_llm_call_trace_appended(self):
        state = _state(
            intent=Intent.CREDIT_BLOCK,
            po_price=100.0, sap_base_price=100.0,
            requester_role="ORDER_MANAGER",
            credit_limit=10_000.0, current_exposure=10_100.0,
        )
        traces_before = len(state.llm_call_traces)
        result = shadow_audit(state)
        l2_traces = [
            t for t in result.llm_call_traces if t.task == "shadow_llm"
        ]
        assert len(l2_traces) == 1


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------


class TestKillSwitch:
    def test_disabled_env_var_skips_l2(self, monkeypatch):
        monkeypatch.setenv("ASOE_SHADOW_LLM_DISABLED", "1")
        state = _state(
            intent=Intent.CREDIT_BLOCK,
            po_price=100.0, sap_base_price=100.0,
            requester_role="ORDER_MANAGER",
            credit_limit=10_000.0, current_exposure=10_100.0,
        )
        result = shadow_audit(state)
        assert result.shadow.status == ShadowStatus.YELLOW
        assert result.shadow.llm_shadow_verdict is None
        # And no shadow_llm trace appended.
        assert all(t.task != "shadow_llm" for t in result.llm_call_traces)

    def test_disabled_truthy_variants(self, monkeypatch):
        for raw in ("true", "TRUE", "yes", "1"):
            monkeypatch.setenv("ASOE_SHADOW_LLM_DISABLED", raw)
            shadow_llm_cache.clear()
            shadow_llm_metrics.reset()
            _reset_l2_shadow_cache()
            state = _state(
                intent=Intent.CREDIT_BLOCK,
                po_price=100.0, sap_base_price=100.0,
                requester_role="ORDER_MANAGER",
                credit_limit=10_000.0, current_exposure=10_100.0,
            )
            result = shadow_audit(state)
            assert result.shadow.llm_shadow_verdict is None, raw


# ---------------------------------------------------------------------------
# Tenant isolation reaches the cache key
# ---------------------------------------------------------------------------


class TestTenantPropagation:
    def test_tenant_id_threaded_to_cache(self):
        state_a = _state(
            intent=Intent.CONTRACTUAL_CORRECTION,
            po_price=500.0, sap_base_price=1000.0,
            tenant_id="tenant-a",
        )
        state_b = _state(
            intent=Intent.CONTRACTUAL_CORRECTION,
            po_price=500.0, sap_base_price=1000.0,
            tenant_id="tenant-b",
        )
        shadow_audit(state_a)
        shadow_audit(state_b)
        # Both miss cache (different tenants → different keys); two
        # cold invocations.
        assert shadow_llm_metrics.invocations_total == 2
        assert shadow_llm_metrics.cache_hits_total == 0

    def test_same_tenant_repeats_hit_cache(self):
        state1 = _state(
            intent=Intent.CONTRACTUAL_CORRECTION,
            po_price=500.0, sap_base_price=1000.0,
            tenant_id="tenant-a",
        )
        state2 = _state(
            intent=Intent.CONTRACTUAL_CORRECTION,
            po_price=500.0, sap_base_price=1000.0,
            tenant_id="tenant-a",
        )
        shadow_audit(state1)
        shadow_audit(state2)
        assert shadow_llm_metrics.cache_hits_total == 1
