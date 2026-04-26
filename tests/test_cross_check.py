from __future__ import annotations

# Coverage for constraints/cross_check.py
#
# The cross-check is a pure function over two IntentDecision objects.
# Tests verify:
#   - Agreement returns the LLM decision unchanged
#   - Disagreement returns the DETERMINISTIC decision + the policy
#     reason code so the orchestration layer can route to
#     MANUAL_REVIEW_REQUIRED
#   - Both intent labels are surfaced for telemetry
#   - The reason code matches contracts/policy.py exactly (test
#     guards against typo drift)

from constraints.cross_check import CrossCheckResult, cross_check
from constraints.specs import IntentDecision
from contracts.policy import LLM_CROSS_CHECK_DISAGREEMENT_REASON


def _decision(intent: str, *, confidence: float = 0.9, rationale: str = "x") -> IntentDecision:
    return IntentDecision(
        intent=intent,
        confidence=confidence,
        rationale=rationale,
    )


def test_agreement_returns_llm_decision() -> None:
    llm = _decision("DUPLICATE_PO", confidence=0.92, rationale="from-llm")
    det = _decision("DUPLICATE_PO", confidence=0.85, rationale="from-deterministic")

    result = cross_check(llm_decision=llm, deterministic_decision=det)

    assert isinstance(result, CrossCheckResult)
    assert result.agreed is True
    assert result.reason is None
    assert result.winning_decision is llm
    assert result.winning_decision.rationale == "from-llm"
    assert result.llm_intent == "DUPLICATE_PO"
    assert result.deterministic_intent == "DUPLICATE_PO"


def test_disagreement_returns_deterministic_decision() -> None:
    llm = _decision("DUPLICATE_PO", confidence=0.95, rationale="from-llm")
    det = _decision("CONTRACTUAL_CORRECTION", confidence=0.88, rationale="from-deterministic")

    result = cross_check(llm_decision=llm, deterministic_decision=det)

    assert result.agreed is False
    assert result.reason == LLM_CROSS_CHECK_DISAGREEMENT_REASON
    # On disagreement the graph stays on the deterministic path
    assert result.winning_decision is det
    # Both labels surfaced for telemetry
    assert result.llm_intent == "DUPLICATE_PO"
    assert result.deterministic_intent == "CONTRACTUAL_CORRECTION"


def test_disagreement_reason_is_exact_policy_constant() -> None:
    """If a future commit changes the reason string by accident,
    this test catches it before it affects audit grepping."""
    assert LLM_CROSS_CHECK_DISAGREEMENT_REASON == "LLM_DETERMINISTIC_DISAGREEMENT"


def test_pure_function_no_mutation() -> None:
    llm = _decision("DUPLICATE_PO")
    det = _decision("CONTRACTUAL_CORRECTION")

    cross_check(llm_decision=llm, deterministic_decision=det)

    # Inputs unchanged — IntentDecision is frozen via Pydantic but
    # confirm anyway as a regression guard.
    assert llm.intent == "DUPLICATE_PO"
    assert det.intent == "CONTRACTUAL_CORRECTION"


def test_result_is_immutable() -> None:
    llm = _decision("DUPLICATE_PO")
    det = _decision("DUPLICATE_PO")
    result = cross_check(llm_decision=llm, deterministic_decision=det)
    # Frozen dataclass → can't reassign fields
    import pytest as _pytest
    with _pytest.raises(Exception):
        result.agreed = False  # type: ignore[misc]
