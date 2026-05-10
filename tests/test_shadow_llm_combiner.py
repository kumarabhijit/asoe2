"""ADR-039 §4.1 — combiner truth-table tests.

Pure-function tests for `compliance.shadow_llm.combine_verdicts`,
which encodes the asymmetric authority rule (L2 can DOWNGRADE only).
The orchestration-side wire-up (in nodes.py / shadow_audit) is
tested separately in `tests/test_shadow_audit_l2_wireup.py`.

Phase X.1 invariant (threshold None) is the default; X.2+ behaviour
fires only when the rollout config flips
`financial_impact_threshold_usd` to a non-null value.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from compliance.shadow_llm import (
    ShadowLLMBundle,
    combine_verdicts,
    load_bundle,
)
from contracts.models import (
    ComplianceDecision,
    ShadowLLMVerdict,
    ShadowStatus,
)


@pytest.fixture
def base_bundle() -> ShadowLLMBundle:
    """Default bundle — X.1 observe-only (threshold = None)."""
    return load_bundle()


@pytest.fixture
def x2_bundle(base_bundle) -> ShadowLLMBundle:
    """X.2 ratified — threshold = $10,000 (high-financial-impact)."""
    return replace(base_bundle, financial_impact_threshold_usd=10_000.0)


@pytest.fixture
def x3_bundle(base_bundle) -> ShadowLLMBundle:
    """X.3 ratified — threshold = $500 (every gating-triggered)."""
    return replace(base_bundle, financial_impact_threshold_usd=500.0)


def _verdict(action: str, **kwargs) -> ShadowLLMVerdict:
    return ShadowLLMVerdict(
        action=action,  # type: ignore[arg-type]
        reason=kwargs.get("reason", "test"),
        confidence=kwargs.get("confidence", 0.8),
        policy_concerns=kwargs.get("policy_concerns", []),
    )


def _decision(status: ShadowStatus) -> ComplianceDecision:
    return ComplianceDecision(status=status)


# ---------------------------------------------------------------------------
# X.1 observe-only — verdict never moves regardless of L2
# ---------------------------------------------------------------------------

class TestX1ObserveOnly:
    @pytest.mark.parametrize("l1", [
        ShadowStatus.GREEN, ShadowStatus.YELLOW, ShadowStatus.RED,
    ])
    @pytest.mark.parametrize("action", [
        "AGREE", "DISAGREE_DOWNGRADE", "ABSTAIN",
    ])
    def test_threshold_none_passes_through(self, base_bundle, l1, action):
        result = combine_verdicts(
            deterministic=_decision(l1),
            llm_verdict=_verdict(action),
            financial_impact_usd=1_000_000,
            bundle=base_bundle,
        )
        assert result == l1


# ---------------------------------------------------------------------------
# X.2 — high-financial-impact downgrade
# ---------------------------------------------------------------------------

class TestX2HighImpactDowngrade:
    def test_green_disagree_above_threshold_downgrades(self, x2_bundle):
        result = combine_verdicts(
            deterministic=_decision(ShadowStatus.GREEN),
            llm_verdict=_verdict("DISAGREE_DOWNGRADE"),
            financial_impact_usd=10_000,
            bundle=x2_bundle,
        )
        assert result == ShadowStatus.YELLOW

    def test_green_disagree_below_threshold_no_downgrade(self, x2_bundle):
        result = combine_verdicts(
            deterministic=_decision(ShadowStatus.GREEN),
            llm_verdict=_verdict("DISAGREE_DOWNGRADE"),
            financial_impact_usd=9_999,
            bundle=x2_bundle,
        )
        assert result == ShadowStatus.GREEN

    def test_green_agree_no_downgrade(self, x2_bundle):
        result = combine_verdicts(
            deterministic=_decision(ShadowStatus.GREEN),
            llm_verdict=_verdict("AGREE"),
            financial_impact_usd=1_000_000,
            bundle=x2_bundle,
        )
        assert result == ShadowStatus.GREEN

    def test_green_abstain_no_downgrade(self, x2_bundle):
        result = combine_verdicts(
            deterministic=_decision(ShadowStatus.GREEN),
            llm_verdict=_verdict("ABSTAIN"),
            financial_impact_usd=1_000_000,
            bundle=x2_bundle,
        )
        assert result == ShadowStatus.GREEN


# ---------------------------------------------------------------------------
# X.3 — broader threshold ($500)
# ---------------------------------------------------------------------------

class TestX3BroadenedThreshold:
    def test_green_disagree_at_500_downgrades(self, x3_bundle):
        result = combine_verdicts(
            deterministic=_decision(ShadowStatus.GREEN),
            llm_verdict=_verdict("DISAGREE_DOWNGRADE"),
            financial_impact_usd=500,
            bundle=x3_bundle,
        )
        assert result == ShadowStatus.YELLOW

    def test_green_disagree_below_500_no_downgrade(self, x3_bundle):
        result = combine_verdicts(
            deterministic=_decision(ShadowStatus.GREEN),
            llm_verdict=_verdict("DISAGREE_DOWNGRADE"),
            financial_impact_usd=499,
            bundle=x3_bundle,
        )
        assert result == ShadowStatus.GREEN


# ---------------------------------------------------------------------------
# Asymmetric authority — L2 cannot upgrade
# ---------------------------------------------------------------------------

class TestAsymmetricAuthority:
    def test_yellow_no_upgrade_via_agree(self, x2_bundle):
        # L2-AGREE on a YELLOW verdict cannot make it GREEN.
        result = combine_verdicts(
            deterministic=_decision(ShadowStatus.YELLOW),
            llm_verdict=_verdict("AGREE"),
            financial_impact_usd=1_000_000,
            bundle=x2_bundle,
        )
        assert result == ShadowStatus.YELLOW

    def test_yellow_disagree_already_at_floor(self, x2_bundle):
        # YELLOW + DISAGREE_DOWNGRADE has no further downgrade path
        # (DISAGREE_DOWNGRADE_TO_RED is explicitly NOT in the schema).
        result = combine_verdicts(
            deterministic=_decision(ShadowStatus.YELLOW),
            llm_verdict=_verdict("DISAGREE_DOWNGRADE"),
            financial_impact_usd=1_000_000,
            bundle=x2_bundle,
        )
        assert result == ShadowStatus.YELLOW

    def test_red_short_circuit(self, x2_bundle):
        # RED is non-negotiable; L2 is never even invoked but
        # defence-in-depth in the combiner anyway.
        result = combine_verdicts(
            deterministic=_decision(ShadowStatus.RED),
            llm_verdict=_verdict("AGREE"),
            financial_impact_usd=1_000_000,
            bundle=x2_bundle,
        )
        assert result == ShadowStatus.RED


# ---------------------------------------------------------------------------
# No L2 verdict (skip / fail-through)
# ---------------------------------------------------------------------------

class TestNoLLMVerdict:
    @pytest.mark.parametrize("l1", [
        ShadowStatus.GREEN, ShadowStatus.YELLOW, ShadowStatus.RED,
    ])
    def test_none_verdict_passes_through(self, x2_bundle, l1):
        result = combine_verdicts(
            deterministic=_decision(l1),
            llm_verdict=None,
            financial_impact_usd=1_000_000,
            bundle=x2_bundle,
        )
        assert result == l1
