from __future__ import annotations

# Phase 1.2 — Intent Classifier tests
#
# Mandatory scenarios (from phase_1_skill_reasoning.md):
#   1. contractual correction
#   2. credit block classification candidate
#   3. mass pricing error
#
# Invariant checks (from CLAUDE.md):
#   - NO recipe is called or returned
#   - NO Compliance Shadow is called or returned
#   - Intent vocabulary is constrained to the allowed enum
#   - Output is IntentDecision: intent + confidence only

import pytest

from contracts.models import Intent
from constraints.specs import IntentDecision
from skills.intent_classifier import IntentClassifier


ALLOWED_INTENTS = {"CONTRACTUAL_CORRECTION", "CREDIT_BLOCK", "MASS_PRICING_ERROR"}


# ---------------------------------------------------------------------------
# Helper: assert result is a plain IntentDecision with no execution artifacts
# ---------------------------------------------------------------------------

def _assert_no_execution_artifacts(result: IntentDecision) -> None:
    """Verify the classifier output carries NO recipe or shadow data."""
    assert not hasattr(result, "recipe_name"), "recipe_name must not be present in Phase 1 output"
    assert not hasattr(result, "shadow_status"), "shadow_status must not be present in Phase 1 output"
    assert not hasattr(result, "execution_log"), "execution_log must not be present in Phase 1 output"


# ---------------------------------------------------------------------------
# Phase 1.2 — three mandatory classification scenarios
# ---------------------------------------------------------------------------

class TestIntentClassifierScenarios:
    """Required by phase_1_skill_reasoning.md: one test per intent path."""

    def test_contractual_correction(self, pricing_event):
        """Normal price variance → CONTRACTUAL_CORRECTION."""
        result = IntentClassifier().classify(pricing_event)
        assert isinstance(result, IntentDecision)
        assert result.intent == "CONTRACTUAL_CORRECTION"
        _assert_no_execution_artifacts(result)

    def test_credit_block_classification_candidate(self, credit_event):
        """Credit fields present → CREDIT_BLOCK candidate."""
        result = IntentClassifier().classify(credit_event)
        assert isinstance(result, IntentDecision)
        assert result.intent == "CREDIT_BLOCK"
        _assert_no_execution_artifacts(result)

    def test_mass_pricing_error(self, mass_event):
        """High line_count → MASS_PRICING_ERROR."""
        result = IntentClassifier().classify(mass_event)
        assert isinstance(result, IntentDecision)
        assert result.intent == "MASS_PRICING_ERROR"
        _assert_no_execution_artifacts(result)


# ---------------------------------------------------------------------------
# Constrained output — vocabulary & shape
# ---------------------------------------------------------------------------

class TestConstrainedOutput:
    def test_intent_within_allowed_vocabulary(self, pricing_event):
        result = IntentClassifier().classify(pricing_event)
        assert result.intent in ALLOWED_INTENTS

    def test_confidence_is_float_in_range(self, pricing_event):
        result = IntentClassifier().classify(pricing_event)
        assert isinstance(result.confidence, float)
        assert 0.0 <= result.confidence <= 1.0

    def test_result_is_intent_decision_type(self, pricing_event):
        result = IntentClassifier().classify(pricing_event)
        assert isinstance(result, IntentDecision)

    def test_all_three_intents_stay_within_vocabulary(self, pricing_event, credit_event, mass_event):
        classifier = IntentClassifier()
        for state in (pricing_event, credit_event, mass_event):
            result = classifier.classify(state)
            assert result.intent in ALLOWED_INTENTS


# ---------------------------------------------------------------------------
# No-execution invariant (CLAUDE.md §1, §2)
# ---------------------------------------------------------------------------

class TestNoExecutionPath:
    def test_classifier_has_no_execute_method(self):
        clf = IntentClassifier()
        assert not hasattr(clf, "execute"), "IntentClassifier must not expose execute()"
        assert not hasattr(clf, "run_recipe"), "IntentClassifier must not expose run_recipe()"

    def test_classifier_has_no_shadow_method(self):
        clf = IntentClassifier()
        assert not hasattr(clf, "audit"), "IntentClassifier must not expose audit()"
        assert not hasattr(clf, "shadow_audit"), "IntentClassifier must not expose shadow_audit()"

    def test_classify_returns_no_recipe_proposal(self, pricing_event):
        result = IntentClassifier().classify(pricing_event)
        _assert_no_execution_artifacts(result)

    def test_classify_does_not_mutate_state(self, pricing_event):
        """Classifier must be pure — state intent must remain UNKNOWN after classify()."""
        before_intent = pricing_event.intent
        IntentClassifier().classify(pricing_event)
        assert pricing_event.intent == before_intent, (
            "IntentClassifier.classify() must not mutate GraphState"
        )


# ---------------------------------------------------------------------------
# Skill loading — verbatim, not summarised
# ---------------------------------------------------------------------------

class TestSkillLoadedVerbatim:
    def test_load_skill_returns_skill_document(self, pricing_event):
        clf = IntentClassifier()
        doc = clf.load_skill(pricing_event.event.event_type)
        assert doc is not None
        assert doc.name == "pricing-reconciliation"

    def test_skill_text_is_verbatim_not_summarised(self, pricing_event):
        clf = IntentClassifier()
        doc = clf.load_skill(pricing_event.event.event_type)
        # Key invariant text must survive verbatim
        assert "Compliance Shadow" in doc.text
        assert "FAIL_TO_HUMAN" in doc.text
        assert "PriceAdjustmentRecipe.py" in doc.text

    def test_skill_lists_expected_recipes(self, pricing_event):
        clf = IntentClassifier()
        doc = clf.load_skill(pricing_event.event.event_type)
        assert "PriceAdjustmentRecipe.py" in doc.recipes
        assert "CreditHoldReleaseRecipe.py" in doc.recipes


# ---------------------------------------------------------------------------
# Backend injection — constrained generation contract
# ---------------------------------------------------------------------------

class TestBackendInjection:
    def test_custom_backend_is_used(self, pricing_event):
        """Backend can be injected for testing without touching env vars."""
        from constraints.fallback_backend import DeterministicFallbackBackend
        backend = DeterministicFallbackBackend()
        clf = IntentClassifier(backend=backend)
        result = clf.classify(pricing_event)
        assert result.intent in ALLOWED_INTENTS

    def test_default_backend_is_deterministic_fallback(self):
        from constraints.fallback_backend import DeterministicFallbackBackend
        clf = IntentClassifier()
        assert isinstance(clf._backend, DeterministicFallbackBackend)
