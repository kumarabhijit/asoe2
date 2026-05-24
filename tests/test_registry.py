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

    def test_registry_contains_price_hold_release_recipe(self):
        assert "PriceHoldReleaseRecipe.py" in REGISTRY

    def test_registry_contains_edi_mismatch_recipe(self):
        assert "EdiMismatchRecipe.py" in REGISTRY

    def test_registry_contains_back_order_recipe(self):
        assert "BackOrderResolutionRecipe.py" in REGISTRY

    def test_registry_contains_over_max_recipe(self):
        assert "OverMaxTrimRecipe.py" in REGISTRY

    def test_registry_contains_moq_recipe(self):
        assert "MOQRoundUpRecipe.py" in REGISTRY

    def test_registry_contains_pallet_config_recipe(self):
        assert "PalletAlignmentRecipe.py" in REGISTRY

    def test_registry_contains_delivery_delay_recipe(self):
        assert "DeliveryDelayResolutionRecipe.py" in REGISTRY

    def test_email_order_entry_recipe_registered(self):
        assert "EmailOrderEntryRecipe.py" in REGISTRY

    def test_registry_size_matches_allowed_recipe_name_literal(self):
        """Registry size stays aligned with AllowedRecipeName. Dynamic
        derivation makes this resilient to future vocabulary expansions
        without requiring a count update."""
        from constraints.specs import AllowedRecipeName
        assert len(REGISTRY) == len(AllowedRecipeName.__args__)

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

    def test_gateway_dependencies_declared(self):
        # ADR-029: tenant_config gateway joins the two original OMS deps
        # (PR #93). Every additional dependency must be declared here so
        # the registry's contract surface stays exact.
        assert len(self.spec.dependencies) == 3
        dep_ops = {d.operation for d in self.spec.dependencies}
        assert "get_fulfillment_status" in dep_ops
        assert "get_matched_po_details" in dep_ops
        assert "resolve_for_event" in dep_ops  # tenant_config (ADR-029)

    def test_gateway_dependencies_target_oms_and_tenant_config(self):
        # The two OMS deps remain; the third dep is tenant_config.
        gateways = {d.gateway_name for d in self.spec.dependencies}
        assert gateways == {"oms", "tenant_config"}

    def test_gateway_effects_declared(self):
        assert len(self.spec.effects) == 1
        assert self.spec.effects[0].gateway_name == "buyer_notification"
        assert self.spec.effects[0].operation == "send"

    def test_gateway_effect_maps_notification_template(self):
        effect = self.spec.effects[0]
        assert "template" in effect.params_from_output
        assert effect.params_from_output["template"] == "notification_template"


# ---------------------------------------------------------------------------
# EmailOrderEntryRecipe spec (ADR-034)
# ---------------------------------------------------------------------------


class TestEmailOrderEntrySpec:
    def setup_method(self):
        self.spec = get_recipe("EmailOrderEntryRecipe.py")

    def test_name_is_correct(self):
        assert self.spec.name == "EmailOrderEntryRecipe.py"

    def test_required_params_present(self):
        for param in (
            "order_id", "customer_id", "composite_confidence",
            "validation_failures", "non_disableable_floor",
            "autonomy_levels", "threshold_auto_approve",
            "threshold_review_band_low", "threshold_auto_correct",
        ):
            assert param in self.spec.required_params

    def test_allowed_intent_is_email_order_entry(self):
        assert "MANUAL_ORDER_INTAKE" in self.spec.allowed_intents

    def test_other_intents_not_in_allowed_intents(self):
        for other in ("DUPLICATE_PO", "CREDIT_BLOCK", "CONTRACTUAL_CORRECTION"):
            assert other not in self.spec.allowed_intents

    def test_func_is_callable(self):
        assert callable(self.spec.func)

    def test_spec_is_frozen(self):
        with pytest.raises((AttributeError, TypeError)):
            self.spec.name = "tampered"  # type: ignore[misc]

    def test_gateway_dependencies_declared(self):
        # ADR-034 Phase B + Phase G: five `email_intake` operations land as
        # required_for_audit=True dependencies — the four non-disable-able
        # floor checks plus fetch_message (Phase G — email source-of-truth
        # substrate for the EmailSourceSection secondary-adapter projection).
        # ADR-042 Phase 3: three Customer Inbox read producers land as
        # required_for_audit=False evidence reads — order_extraction
        # (extract_order + extract_entities) and sap_order (validate) — whose
        # output activates the Order Entry / Entities / SAP Data tabs.
        assert len(self.spec.dependencies) == 8
        gateways = {d.gateway_name for d in self.spec.dependencies}
        assert gateways == {"email_intake", "order_extraction", "sap_order"}
        ops = {d.operation for d in self.spec.dependencies}
        assert ops == {
            "sender_auth", "resolve_customer",
            "duplicate_po_pre_check", "credit_check",
            "fetch_message",
            "extract_order", "extract_entities", "validate",
        }
        audit_required = {
            d.result_key for d in self.spec.dependencies if d.required_for_audit
        }
        assert audit_required == {
            "sender_auth_context", "customer_resolution_context",
            "duplicate_po_pre_check_context", "credit_check_context",
            "email_source_context",
        }
        # The inbox read producers are evidence-enriching, not lifecycle-gating.
        soft = {
            d.result_key for d in self.spec.dependencies if not d.required_for_audit
        }
        assert soft == {"order_entry_extraction", "inbox_entities", "sap_data"}

    def test_no_effects(self):
        # No gateway side effects in Phase B — REQUEST_CLARIFICATION /
        # ESCALATE / REJECT actions don't trigger automated buyer
        # notifications today (deferred to a downstream notification
        # surface; not in scope for this skill's recipe).
        assert self.spec.effects == ()

    def test_expected_metadata_keys_declared(self):
        assert "composite_confidence" in self.spec.expected_metadata_keys
        assert "non_disableable_floor" in self.spec.expected_metadata_keys
        assert "validation_failures" in self.spec.expected_metadata_keys


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
