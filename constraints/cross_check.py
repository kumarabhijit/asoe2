from __future__ import annotations

# Cross-check: agreement gate between LLM and deterministic intent.
#
# When an LLM-served `classify_intent` produces a different intent
# than the deterministic classifier would have produced, we do NOT
# trust the LLM and instead route the exception to
# MANUAL_REVIEW_REQUIRED. This is the conservative shakeout posture
# the user asked for: the deterministic branch is always cheap to
# run and the disagreement is a high-signal "don't auto-execute"
# trigger.
#
# Mechanics:
#   - The orchestration `classify` node calls the active backend
#     (LLM or deterministic) AND, when the active backend is NOT
#     the deterministic one, the deterministic backend too.
#   - If the two intents agree → use the active result.
#   - If they disagree → return CrossCheckResult with `disagreed=True`
#     and the reason `LLM_DETERMINISTIC_DISAGREEMENT`. The orch
#     layer translates this into MANUAL_REVIEW_REQUIRED.
#
# This module is intentionally minimal — pure function over two
# IntentDecision objects. It does NOT call any backend itself; the
# orchestration layer (S4) collects both decisions and asks
# `cross_check()` for the verdict.
#
# Cost: ≈free. The deterministic classifier is if/elif over the
# event payload; CPU only, no network. The LLM call cost is the
# same as without cross-check; we just refuse to act on its result
# when it diverges from the rule path.

from dataclasses import dataclass

from contracts.policy import LLM_CROSS_CHECK_DISAGREEMENT_REASON
from constraints.specs import IntentDecision


@dataclass(frozen=True)
class CrossCheckResult:
    """Outcome of comparing LLM and deterministic intent decisions.

    Fields:
        agreed: True when both classifiers produced the same intent.
        winning_decision: the decision the graph should act on.
            On agreement → the LLM decision (same as deterministic
            modulo confidence/rationale). On disagreement → the
            DETERMINISTIC decision (so the graph stays on the
            deterministic path while a human reviews).
        reason: short policy code, populated only on disagreement.
            Used by the orch layer to set ExecutionLog.errors and
            route to MANUAL_REVIEW_REQUIRED.
        llm_intent / deterministic_intent: surfaced for telemetry
            so the disagreement is logged with both labels.
    """

    agreed: bool
    winning_decision: IntentDecision
    reason: str | None
    llm_intent: str
    deterministic_intent: str


def cross_check(
    *,
    llm_decision: IntentDecision,
    deterministic_decision: IntentDecision,
) -> CrossCheckResult:
    """Compare an LLM-produced IntentDecision against the deterministic
    one and return the cross-check verdict.

    The function is pure — no logging, no side effects. The
    orchestration layer is responsible for emitting the structured
    log line + setting MANUAL_REVIEW_REQUIRED when `agreed=False`.
    """
    if llm_decision.intent == deterministic_decision.intent:
        return CrossCheckResult(
            agreed=True,
            winning_decision=llm_decision,
            reason=None,
            llm_intent=llm_decision.intent,
            deterministic_intent=deterministic_decision.intent,
        )

    return CrossCheckResult(
        agreed=False,
        # Stay on the deterministic path during disagreement — the
        # graph routes to MANUAL_REVIEW_REQUIRED based on `reason`,
        # but we still need a typed IntentDecision for downstream
        # nodes in case the routing falls through to recipe
        # selection (e.g. legacy callers that ignore `reason`).
        winning_decision=deterministic_decision,
        reason=LLM_CROSS_CHECK_DISAGREEMENT_REASON,
        llm_intent=llm_decision.intent,
        deterministic_intent=deterministic_decision.intent,
    )


__all__ = ("CrossCheckResult", "cross_check")
