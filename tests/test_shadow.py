from __future__ import annotations

# Phase 2 — Compliance Shadow tests
#
# Covers (from phase_2_compliance_shadow.md and tasks.md §2.1 / §2.2):
#   - ComplianceShadow satisfies the ComplianceShadowBase interface
#   - audit(): GREEN / YELLOW / RED paths, TraceID, reasons, policy_hits,
#              constrained_by, backend injection
#   - enforce(): PROCEED on GREEN, ESCALATE on YELLOW, BLOCK on RED,
#                explanation attached, trace_id propagated
#   - No-execution invariant: no recipe method on ComplianceShadow
#   - Backend is injectable for isolated testing

import uuid

import pytest

from compliance.shadow import ComplianceShadow, ComplianceShadowBase
from constraints.fallback_backend import DeterministicFallbackBackend
from contracts.models import (
    ComplianceDecision,
    Intent,
    ShadowEnforcement,
    ShadowStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _shadow() -> ComplianceShadow:
    """Return a ComplianceShadow using the deterministic fallback (no LLM)."""
    return ComplianceShadow(backend=DeterministicFallbackBackend())


def _is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Interface contract (tasks.md §2.1 "define a ComplianceShadow interface")
# ---------------------------------------------------------------------------

class TestComplianceShadowInterface:
    def test_implements_base(self):
        assert isinstance(ComplianceShadow(), ComplianceShadowBase)

    def test_has_audit_method(self):
        assert callable(getattr(ComplianceShadow, "audit", None))

    def test_has_enforce_method(self):
        assert callable(getattr(ComplianceShadow, "enforce", None))

    def test_base_is_abstract(self):
        """ComplianceShadowBase cannot be instantiated directly."""
        with pytest.raises(TypeError):
            ComplianceShadowBase()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# audit() — GREEN path
# ---------------------------------------------------------------------------

class TestAuditGreen:
    def test_returns_compliance_decision(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        result = _shadow().audit(pricing_event)
        assert isinstance(result, ComplianceDecision)

    def test_status_is_green(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        result = _shadow().audit(pricing_event)
        assert result.status == ShadowStatus.GREEN

    def test_trace_id_is_valid_uuid(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        result = _shadow().audit(pricing_event)
        assert _is_valid_uuid(result.trace_id)

    def test_trace_id_is_unique_per_call(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        s = _shadow()
        r1 = s.audit(pricing_event)
        r2 = s.audit(pricing_event)
        assert r1.trace_id != r2.trace_id

    def test_reasons_are_non_empty(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        result = _shadow().audit(pricing_event)
        assert isinstance(result.reasons, list) and len(result.reasons) > 0

    def test_policy_hits_are_empty_on_green(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        result = _shadow().audit(pricing_event)
        assert result.policy_hits == []

    def test_constrained_by_is_set(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        result = _shadow().audit(pricing_event)
        assert result.constrained_by is not None and len(result.constrained_by) > 0


# ---------------------------------------------------------------------------
# audit() — YELLOW path
# ---------------------------------------------------------------------------

class TestAuditYellow:
    def test_status_is_yellow(self, credit_event):
        credit_event.intent = Intent.CREDIT_BLOCK
        result = _shadow().audit(credit_event)
        assert result.status == ShadowStatus.YELLOW

    def test_policy_hit_credit_release_review(self, credit_event):
        credit_event.intent = Intent.CREDIT_BLOCK
        result = _shadow().audit(credit_event)
        assert "CREDIT_RELEASE_REVIEW" in result.policy_hits

    def test_trace_id_is_valid_uuid(self, credit_event):
        credit_event.intent = Intent.CREDIT_BLOCK
        result = _shadow().audit(credit_event)
        assert _is_valid_uuid(result.trace_id)

    def test_reasons_non_empty(self, credit_event):
        credit_event.intent = Intent.CREDIT_BLOCK
        result = _shadow().audit(credit_event)
        assert len(result.reasons) > 0


# ---------------------------------------------------------------------------
# audit() — RED path
# ---------------------------------------------------------------------------

class TestAuditRed:
    def test_status_is_red_for_mass_error(self, mass_event):
        mass_event.intent = Intent.MASS_PRICING_ERROR
        result = _shadow().audit(mass_event)
        assert result.status == ShadowStatus.RED

    def test_policy_hits_present_on_red(self, mass_event):
        mass_event.intent = Intent.MASS_PRICING_ERROR
        result = _shadow().audit(mass_event)
        assert len(result.policy_hits) > 0

    def test_trace_id_is_valid_uuid(self, mass_event):
        mass_event.intent = Intent.MASS_PRICING_ERROR
        result = _shadow().audit(mass_event)
        assert _is_valid_uuid(result.trace_id)

    def test_reasons_non_empty_on_red(self, mass_event):
        mass_event.intent = Intent.MASS_PRICING_ERROR
        result = _shadow().audit(mass_event)
        assert len(result.reasons) > 0

    def test_red_on_high_batch_variance(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        pricing_event.batch_total_variance = 15_000.0
        result = _shadow().audit(pricing_event)
        assert result.status == ShadowStatus.RED
        assert "CIRCUIT_BREAKER_VARIANCE" in result.policy_hits


# ---------------------------------------------------------------------------
# enforce() — PROCEED on GREEN (tasks.md §2.2)
# ---------------------------------------------------------------------------

class TestEnforceGreen:
    def _green_decision(self) -> ComplianceDecision:
        return ComplianceDecision(
            status=ShadowStatus.GREEN,
            reasons=["No blocking policy hit."],
            policy_hits=[],
            constrained_by="Guidance/Outlines fallback schema",
        )

    def test_returns_shadow_enforcement(self):
        result = _shadow().enforce(self._green_decision())
        assert isinstance(result, ShadowEnforcement)

    def test_action_is_proceed(self):
        result = _shadow().enforce(self._green_decision())
        assert result.action == "PROCEED"

    def test_trace_id_propagated(self):
        decision = self._green_decision()
        result = _shadow().enforce(decision)
        assert result.trace_id == decision.trace_id

    def test_explanation_is_non_empty(self):
        result = _shadow().enforce(self._green_decision())
        assert len(result.explanation) > 0

    def test_explanation_mentions_green(self):
        result = _shadow().enforce(self._green_decision())
        assert "GREEN" in result.explanation


# ---------------------------------------------------------------------------
# enforce() — ESCALATE on YELLOW (tasks.md §2.2)
# ---------------------------------------------------------------------------

class TestEnforceYellow:
    def _yellow_decision(self) -> ComplianceDecision:
        return ComplianceDecision(
            status=ShadowStatus.YELLOW,
            reasons=["Credit path requires manual review by policy."],
            policy_hits=["CREDIT_RELEASE_REVIEW"],
            constrained_by="Guidance/Outlines fallback schema",
        )

    def test_action_is_escalate(self):
        result = _shadow().enforce(self._yellow_decision())
        assert result.action == "ESCALATE"

    def test_trace_id_propagated(self):
        decision = self._yellow_decision()
        result = _shadow().enforce(decision)
        assert result.trace_id == decision.trace_id

    def test_explanation_mentions_yellow(self):
        result = _shadow().enforce(self._yellow_decision())
        assert "YELLOW" in result.explanation

    def test_explanation_mentions_manual_review(self):
        result = _shadow().enforce(self._yellow_decision())
        assert "MANUAL_REVIEW" in result.explanation

    def test_reason_text_included_in_explanation(self):
        decision = self._yellow_decision()
        result = _shadow().enforce(decision)
        assert "Credit path" in result.explanation


# ---------------------------------------------------------------------------
# enforce() — BLOCK on RED (tasks.md §2.2)
# ---------------------------------------------------------------------------

class TestEnforceRed:
    def _red_decision(self) -> ComplianceDecision:
        return ComplianceDecision(
            status=ShadowStatus.RED,
            reasons=["Systemic pricing failure requires human escalation."],
            policy_hits=["HITL_REQUIRED_FOR_SYSTEMIC_FAILURE"],
            constrained_by="Guidance/Outlines fallback schema",
        )

    def test_action_is_block(self):
        result = _shadow().enforce(self._red_decision())
        assert result.action == "BLOCK"

    def test_trace_id_propagated(self):
        decision = self._red_decision()
        result = _shadow().enforce(decision)
        assert result.trace_id == decision.trace_id

    def test_explanation_mentions_red(self):
        result = _shadow().enforce(self._red_decision())
        assert "RED" in result.explanation

    def test_explanation_mentions_halt(self):
        result = _shadow().enforce(self._red_decision())
        assert "halted" in result.explanation.lower() or "halt" in result.explanation.lower()

    def test_reason_text_included_in_explanation(self):
        decision = self._red_decision()
        result = _shadow().enforce(decision)
        assert "Systemic pricing failure" in result.explanation


# ---------------------------------------------------------------------------
# audit() + enforce() end-to-end (GREEN / YELLOW / RED)
# ---------------------------------------------------------------------------

class TestAuditThenEnforce:
    def test_green_path_end_to_end(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        shadow = _shadow()
        decision = shadow.audit(pricing_event)
        enforcement = shadow.enforce(decision)
        assert enforcement.action == "PROCEED"
        assert enforcement.trace_id == decision.trace_id

    def test_yellow_path_end_to_end(self, credit_event):
        credit_event.intent = Intent.CREDIT_BLOCK
        shadow = _shadow()
        decision = shadow.audit(credit_event)
        enforcement = shadow.enforce(decision)
        assert enforcement.action == "ESCALATE"
        assert enforcement.trace_id == decision.trace_id

    def test_red_path_end_to_end(self, mass_event):
        mass_event.intent = Intent.MASS_PRICING_ERROR
        shadow = _shadow()
        decision = shadow.audit(mass_event)
        enforcement = shadow.enforce(decision)
        assert enforcement.action == "BLOCK"
        assert enforcement.trace_id == decision.trace_id


# ---------------------------------------------------------------------------
# No-execution invariant (CLAUDE.md §1 / §2)
# ---------------------------------------------------------------------------

class TestNoExecutionPath:
    def test_shadow_has_no_execute_method(self):
        s = ComplianceShadow()
        assert not hasattr(s, "execute"), "ComplianceShadow must not expose execute()"
        assert not hasattr(s, "run_recipe"), "ComplianceShadow must not expose run_recipe()"

    def test_shadow_has_no_recipe_selection(self):
        s = ComplianceShadow()
        assert not hasattr(s, "select_recipe"), "ComplianceShadow must not select recipes"
        assert not hasattr(s, "propose_recipe"), "ComplianceShadow must not propose recipes"

    def test_enforce_does_not_return_recipe_name(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        shadow = _shadow()
        decision = shadow.audit(pricing_event)
        enforcement = shadow.enforce(decision)
        assert not hasattr(enforcement, "recipe_name")
        assert not hasattr(enforcement, "invocation")


# ---------------------------------------------------------------------------
# Backend injection
# ---------------------------------------------------------------------------

class TestBackendInjection:
    def test_custom_backend_accepted(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        backend = DeterministicFallbackBackend()
        shadow = ComplianceShadow(backend=backend)
        result = shadow.audit(pricing_event)
        assert isinstance(result, ComplianceDecision)

    def test_default_backend_is_fallback(self):
        shadow = ComplianceShadow()
        assert isinstance(shadow.backend, DeterministicFallbackBackend)
