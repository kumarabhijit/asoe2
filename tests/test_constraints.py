from __future__ import annotations

import re
from typing import get_args

import pytest

from constraints.fallback_backend import DeterministicFallbackBackend
from constraints.guidance_backend import GuidanceRegexBackend
from constraints.specs import AllowedIntent, AllowedRecipeName, AllowedResolutionAction, AllowedShadowStatus
from contracts.models import Intent


# ---------------------------------------------------------------------------
# DeterministicFallbackBackend — intent classification
# ---------------------------------------------------------------------------

class TestFallbackIntentClassification:
    def test_pricing_event_classifies_as_contractual_correction(self, pricing_event):
        result = DeterministicFallbackBackend().classify_intent(pricing_event)
        assert result.intent == "CONTRACTUAL_CORRECTION"

    def test_credit_event_classifies_as_credit_block(self, credit_event):
        result = DeterministicFallbackBackend().classify_intent(credit_event)
        assert result.intent == "CREDIT_BLOCK"

    def test_mass_event_classifies_as_mass_pricing_error(self, mass_event):
        result = DeterministicFallbackBackend().classify_intent(mass_event)
        assert result.intent == "MASS_PRICING_ERROR"

    def test_intent_is_within_allowed_vocabulary(self, pricing_event):
        allowed = {"CONTRACTUAL_CORRECTION", "CREDIT_BLOCK", "MASS_PRICING_ERROR"}
        result = DeterministicFallbackBackend().classify_intent(pricing_event)
        assert result.intent in allowed

    def test_confidence_is_bounded(self, pricing_event):
        result = DeterministicFallbackBackend().classify_intent(pricing_event)
        assert 0.0 <= result.confidence <= 1.0

    def test_rationale_is_present(self, pricing_event):
        result = DeterministicFallbackBackend().classify_intent(pricing_event)
        assert result.rationale is not None and len(result.rationale) > 0


# ---------------------------------------------------------------------------
# DeterministicFallbackBackend — shadow decision
# ---------------------------------------------------------------------------

class TestFallbackShadowDecision:
    def test_normal_pricing_returns_green(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        result = DeterministicFallbackBackend().shadow_decision(pricing_event)
        assert result.status == "GREEN"
        assert result.policy_hits == []

    def test_credit_block_returns_yellow(self, credit_event):
        credit_event.intent = Intent.CREDIT_BLOCK
        result = DeterministicFallbackBackend().shadow_decision(credit_event)
        assert result.status == "YELLOW"
        assert "CREDIT_RELEASE_REVIEW" in result.policy_hits

    def test_mass_error_returns_red(self, mass_event):
        mass_event.intent = Intent.MASS_PRICING_ERROR
        result = DeterministicFallbackBackend().shadow_decision(mass_event)
        assert result.status == "RED"
        assert result.policy_hits

    def test_high_variance_returns_red(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        pricing_event.batch_total_variance = 11_000.0
        result = DeterministicFallbackBackend().shadow_decision(pricing_event)
        assert result.status == "RED"
        assert "CIRCUIT_BREAKER_VARIANCE" in result.policy_hits

    def test_shadow_status_within_allowed_vocabulary(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        result = DeterministicFallbackBackend().shadow_decision(pricing_event)
        assert result.status in {"GREEN", "YELLOW", "RED"}

    def test_reasons_list_is_non_empty(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        result = DeterministicFallbackBackend().shadow_decision(pricing_event)
        assert isinstance(result.reasons, list) and len(result.reasons) > 0


# ---------------------------------------------------------------------------
# DeterministicFallbackBackend — recipe proposal
# ---------------------------------------------------------------------------

class TestFallbackRecipeProposal:
    def test_contractual_correction_proposes_price_recipe(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        result = DeterministicFallbackBackend().propose_recipe(pricing_event)
        assert result is not None
        assert result.recipe_name == "PriceAdjustmentRecipe.py"

    def test_credit_block_proposes_credit_recipe(self, credit_event):
        credit_event.intent = Intent.CREDIT_BLOCK
        result = DeterministicFallbackBackend().propose_recipe(credit_event)
        assert result is not None
        assert result.recipe_name == "CreditHoldReleaseRecipe.py"

    def test_mass_error_returns_no_recipe(self, mass_event):
        mass_event.intent = Intent.MASS_PRICING_ERROR
        result = DeterministicFallbackBackend().propose_recipe(mass_event)
        assert result is None

    def test_proposed_recipe_is_within_allowed_vocabulary(self, pricing_event):
        pricing_event.intent = Intent.CONTRACTUAL_CORRECTION
        allowed = {"PriceAdjustmentRecipe.py", "CreditHoldReleaseRecipe.py"}
        result = DeterministicFallbackBackend().propose_recipe(pricing_event)
        assert result is not None
        assert result.recipe_name in allowed


# ---------------------------------------------------------------------------
# GuidanceRegexBackend — constrained generation patterns
# ---------------------------------------------------------------------------

class TestGuidanceRegexBackend:
    def test_intent_regex_is_correct(self):
        g = GuidanceRegexBackend()
        # Dynamic check — derives expected from AllowedIntent.__args__ so
        # the assertion stays correct as new intents are added. The regex
        # must list every AllowedIntent value separated by `|`.
        from constraints.specs import AllowedIntent
        expected = "|".join(AllowedIntent.__args__)
        assert g.intent_regex() == expected

    def test_shadow_verdict_regex_is_correct(self):
        g = GuidanceRegexBackend()
        assert g.shadow_verdict_regex() == r"GREEN|YELLOW|RED"

    def test_recipe_name_regex_is_correct(self):
        g = GuidanceRegexBackend()
        # Dynamic check — derives expected from AllowedRecipeName.__args__.
        from constraints.specs import AllowedRecipeName
        expected = "|".join(r.replace(".", r"\.") for r in AllowedRecipeName.__args__)
        assert g.recipe_name_regex() == expected

    def test_intent_regex_matches_all_allowed_values(self):
        pattern = re.compile(GuidanceRegexBackend().intent_regex())
        for value in ("CONTRACTUAL_CORRECTION", "CREDIT_BLOCK", "MASS_PRICING_ERROR"):
            assert pattern.fullmatch(value), f"Pattern did not match {value}"

    def test_intent_regex_rejects_unknown(self):
        pattern = re.compile(GuidanceRegexBackend().intent_regex())
        assert not pattern.fullmatch("UNKNOWN")
        assert not pattern.fullmatch("FREE_TEXT")

    def test_shadow_verdict_regex_matches_all_allowed_values(self):
        pattern = re.compile(GuidanceRegexBackend().shadow_verdict_regex())
        for value in ("GREEN", "YELLOW", "RED"):
            assert pattern.fullmatch(value)

    def test_shadow_verdict_regex_rejects_unknown(self):
        pattern = re.compile(GuidanceRegexBackend().shadow_verdict_regex())
        assert not pattern.fullmatch("ORANGE")
        assert not pattern.fullmatch("")

    def test_recipe_regex_matches_allowed_names(self):
        pattern = re.compile(GuidanceRegexBackend().recipe_name_regex())
        for name in ("PriceAdjustmentRecipe.py", "CreditHoldReleaseRecipe.py"):
            assert pattern.fullmatch(name)

    def test_recipe_regex_rejects_unknown_names(self):
        pattern = re.compile(GuidanceRegexBackend().recipe_name_regex())
        assert not pattern.fullmatch("SomeOtherRecipe.py")
        assert not pattern.fullmatch("")

    def test_resolution_action_regex_matches_all_six_actions(self):
        pattern = re.compile(GuidanceRegexBackend().resolution_action_regex())
        for action in (
            "BLOCK_AND_NOTIFY", "MERGE", "SUPERSEDE",
            "ALLOW_BOTH", "ESCALATE", "REQUEST_BUYER_CONFIRMATION",
        ):
            assert pattern.fullmatch(action), f"Pattern did not match {action}"

    def test_resolution_action_regex_rejects_old_actions(self):
        pattern = re.compile(GuidanceRegexBackend().resolution_action_regex())
        assert not pattern.fullmatch("ANNOTATE_AND_PASS")
        assert not pattern.fullmatch("ALLOW")


# ---------------------------------------------------------------------------
# Vocabulary sync — GuidanceRegexBackend ↔ AllowedIntent / AllowedShadowStatus
#                   / AllowedRecipeName Literals
#
# These tests guard against silent drift: if a new value is added to a Literal
# but the corresponding GuidanceRegexBackend method is not updated (or vice
# versa), the constrained generation boundary and the Pydantic validation
# boundary would accept different vocabularies.  That would mean a value
# generated by the regex backend could be rejected by Pydantic, or a value
# allowed by Pydantic could never be generated by the regex pattern.
# ---------------------------------------------------------------------------

class TestVocabularySyncGuidanceToLiterals:
    """GuidanceRegexBackend regex must match every value in the corresponding
    AllowedIntent / AllowedShadowStatus / AllowedRecipeName Literal — no more,
    no less (fullmatch only, no partial matches counted)."""

    def test_intent_regex_covers_all_allowed_intent_values(self):
        pattern = re.compile(GuidanceRegexBackend().intent_regex())
        for value in get_args(AllowedIntent):
            assert pattern.fullmatch(value), (
                f"intent_regex() does not cover AllowedIntent value: {value!r}"
            )

    def test_intent_regex_rejects_values_outside_allowed_intent(self):
        """Regex must not produce values absent from AllowedIntent."""
        allowed = set(get_args(AllowedIntent))
        pattern = re.compile(GuidanceRegexBackend().intent_regex())
        for unknown in ("UNKNOWN", "FREE_FORM", "", "contractual_correction"):
            assert not pattern.fullmatch(unknown) or unknown in allowed, (
                f"intent_regex() accepted {unknown!r} which is not in AllowedIntent"
            )

    def test_shadow_verdict_regex_covers_all_allowed_shadow_status_values(self):
        pattern = re.compile(GuidanceRegexBackend().shadow_verdict_regex())
        for value in get_args(AllowedShadowStatus):
            assert pattern.fullmatch(value), (
                f"shadow_verdict_regex() does not cover AllowedShadowStatus value: {value!r}"
            )

    def test_recipe_regex_covers_all_allowed_recipe_name_values(self):
        pattern = re.compile(GuidanceRegexBackend().recipe_name_regex())
        for value in get_args(AllowedRecipeName):
            assert pattern.fullmatch(value), (
                f"recipe_name_regex() does not cover AllowedRecipeName value: {value!r}"
            )

    def test_allowed_recipe_names_match_registry_keys(self):
        """AllowedRecipeName Literal values must equal the recipe REGISTRY keys.

        The constrained generation boundary (Pydantic AllowedRecipeName) and
        the execution registry (recipes/registry.py REGISTRY) must accept
        exactly the same set of names.  A mismatch means either:
          - a recipe exists that can never be generated (dead code), or
          - a recipe can be generated but cannot be executed (runtime KeyError).
        """
        from recipes.registry import REGISTRY
        literal_names = set(get_args(AllowedRecipeName))
        registry_names = set(REGISTRY.keys())
        assert literal_names == registry_names, (
            f"AllowedRecipeName Literal {literal_names} does not match "
            f"REGISTRY keys {registry_names}"
        )

    def test_allowed_intent_values_match_intent_enum(self):
        """AllowedIntent Literal values must be a subset of the Intent enum.

        Every value the constrained generation can produce must be a valid
        Intent enum member so that Intent(decision.intent) never raises.
        """
        intent_values = {m.value for m in Intent if m != Intent.UNKNOWN}
        for value in get_args(AllowedIntent):
            assert value in intent_values, (
                f"AllowedIntent value {value!r} is not a member of the Intent enum"
            )

    def test_resolution_action_regex_covers_all_allowed_values(self):
        pattern = re.compile(GuidanceRegexBackend().resolution_action_regex())
        for value in get_args(AllowedResolutionAction):
            assert pattern.fullmatch(value), (
                f"resolution_action_regex() does not cover AllowedResolutionAction value: {value!r}"
            )

    def test_resolution_action_regex_rejects_unknown(self):
        pattern = re.compile(GuidanceRegexBackend().resolution_action_regex())
        for unknown in ("ANNOTATE_AND_PASS", "ALLOW", "DENY", ""):
            assert not pattern.fullmatch(unknown), (
                f"resolution_action_regex() accepted {unknown!r} which is not a valid resolution action"
            )
