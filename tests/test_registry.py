from __future__ import annotations

# Phase 3.1 — Recipe Registry tests
#
# Verifies that:
#   - Both recipes are registered with correct metadata
#   - Required params are complete for each recipe
#   - Allowed intents are correct for each recipe
#   - get_recipe() rejects unknown names with KeyError
#   - RecipeSpec is frozen (immutable) — no mutation allowed
#   - Registry vocabulary matches AllowedRecipeName from constraints/specs.py

import pytest

from recipes.registry import REGISTRY, RecipeSpec, get_recipe
from constraints.specs import AllowedRecipeName


# ---------------------------------------------------------------------------
# Registry completeness
# ---------------------------------------------------------------------------

class TestRegistryCompleteness:
    def test_registry_contains_price_recipe(self):
        assert "PriceAdjustmentRecipe.py" in REGISTRY

    def test_registry_contains_credit_recipe(self):
        assert "CreditHoldReleaseRecipe.py" in REGISTRY

    def test_registry_has_exactly_three_recipes(self):
        assert len(REGISTRY) == 3

    def test_registry_names_match_allowed_recipe_name_literal(self):
        """Registry keys must stay in sync with AllowedRecipeName in specs.py."""
        allowed = set(AllowedRecipeName.__args__)  # type: ignore[attr-defined]
        assert set(REGISTRY.keys()) == allowed


# ---------------------------------------------------------------------------
# PriceAdjustmentRecipe spec
# ---------------------------------------------------------------------------

class TestPriceAdjustmentSpec:
    def setup_method(self):
        self.spec = get_recipe("PriceAdjustmentRecipe.py")

    def test_name_is_correct(self):
        assert self.spec.name == "PriceAdjustmentRecipe.py"

    def test_required_params_present(self):
        for param in ("order_id", "line_item", "requested_price", "erp_context"):
            assert param in self.spec.required_params

    def test_allowed_intent_is_contractual_correction(self):
        assert "CONTRACTUAL_CORRECTION" in self.spec.allowed_intents

    def test_credit_block_not_in_allowed_intents(self):
        assert "CREDIT_BLOCK" not in self.spec.allowed_intents

    def test_func_is_callable(self):
        assert callable(self.spec.func)

    def test_spec_is_frozen(self):
        """RecipeSpec must be immutable — no field can be overwritten."""
        with pytest.raises((AttributeError, TypeError)):
            self.spec.name = "tampered"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CreditHoldReleaseRecipe spec
# ---------------------------------------------------------------------------

class TestCreditHoldReleaseSpec:
    def setup_method(self):
        self.spec = get_recipe("CreditHoldReleaseRecipe.py")

    def test_name_is_correct(self):
        assert self.spec.name == "CreditHoldReleaseRecipe.py"

    def test_required_params_present(self):
        for param in ("order_id", "requester_role", "credit_limit", "current_exposure"):
            assert param in self.spec.required_params

    def test_allowed_intent_is_credit_block(self):
        assert "CREDIT_BLOCK" in self.spec.allowed_intents

    def test_contractual_correction_not_in_allowed_intents(self):
        assert "CONTRACTUAL_CORRECTION" not in self.spec.allowed_intents

    def test_func_is_callable(self):
        assert callable(self.spec.func)

    def test_spec_is_frozen(self):
        with pytest.raises((AttributeError, TypeError)):
            self.spec.name = "tampered"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DuplicatePORecipe spec
# ---------------------------------------------------------------------------

class TestDuplicatePOSpec:
    def setup_method(self):
        self.spec = get_recipe("DuplicatePORecipe.py")

    def test_name_is_correct(self):
        assert self.spec.name == "DuplicatePORecipe.py"

    def test_required_params_present(self):
        for param in ("incoming_po_number", "customer_id", "signal_scores"):
            assert param in self.spec.required_params

    def test_allowed_intent_is_duplicate_po(self):
        assert "DUPLICATE_PO" in self.spec.allowed_intents

    def test_contractual_correction_not_in_allowed_intents(self):
        assert "CONTRACTUAL_CORRECTION" not in self.spec.allowed_intents

    def test_credit_block_not_in_allowed_intents(self):
        assert "CREDIT_BLOCK" not in self.spec.allowed_intents

    def test_func_is_callable(self):
        assert callable(self.spec.func)

    def test_spec_is_frozen(self):
        with pytest.raises((AttributeError, TypeError)):
            self.spec.name = "tampered"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# get_recipe() — lookup and rejection
# ---------------------------------------------------------------------------

class TestGetRecipe:
    def test_returns_recipe_spec_type(self):
        spec = get_recipe("PriceAdjustmentRecipe.py")
        assert isinstance(spec, RecipeSpec)

    def test_unknown_recipe_raises_key_error(self):
        with pytest.raises(KeyError):
            get_recipe("UnknownRecipe.py")

    def test_empty_string_raises_key_error(self):
        with pytest.raises(KeyError):
            get_recipe("")

    def test_partial_name_raises_key_error(self):
        with pytest.raises(KeyError):
            get_recipe("PriceAdjustment")

    def test_case_sensitive_rejection(self):
        with pytest.raises(KeyError):
            get_recipe("priceadjustmentrecipe.py")

    def test_both_recipes_retrievable(self):
        p = get_recipe("PriceAdjustmentRecipe.py")
        c = get_recipe("CreditHoldReleaseRecipe.py")
        assert p.name != c.name
