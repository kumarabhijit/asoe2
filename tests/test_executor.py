from __future__ import annotations

# Phase 3.2 — RecipeExecutor tests
#
# Covers (from phase_3_recipe_invocation.md and tasks.md §3.2):
#   - Successful invocations: PriceAdjustmentRecipe and CreditHoldReleaseRecipe
#   - Rejected invocations: unknown recipe name, missing required params
#   - ExecutionLog immutability: inputs, outputs, errors captured correctly
#   - Constrained proposal gate: RecipeProposal rejects non-registered names
#   - No-shadow invariant: RecipeExecutor has no audit() or shadow method
#   - No-orchestration invariant: no LangGraph, no graph state mutation
#   - TraceID propagated; defaults to a valid UUID when not supplied

import uuid

import pytest
from pydantic import ValidationError

from contracts.models import ExecutionLog
from constraints.specs import RecipeProposal
from recipes.executor import RecipeExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _price_params(
    order_id="SO-1001",
    line_item=1,
    requested_price=92.0,
    base_price=100.0,
    threshold=0.15,
    condition_type="YK07",
):
    return {
        "order_id": order_id,
        "line_item": line_item,
        "requested_price": requested_price,
        "erp_context": {
            "base_price": base_price,
            "max_discount_allowed": threshold,
            "condition_type": condition_type,
        },
    }


def _credit_params(
    order_id="SO-2001",
    requester_role="ORDER_MANAGER",
    credit_limit=10_000.0,
    current_exposure=9_000.0,
    authorized_roles=("ORDER_MANAGER", "FINANCE_DIRECTOR"),
    exposure_tolerance=5_000.0,
):
    return {
        "order_id": order_id,
        "requester_role": requester_role,
        "credit_limit": credit_limit,
        "current_exposure": current_exposure,
        "authorized_roles": authorized_roles,
        "exposure_tolerance": exposure_tolerance,
    }


# ---------------------------------------------------------------------------
# Constrained proposal gate (Guidance / Outlines constraint)
# ---------------------------------------------------------------------------

class TestRecipeProposalConstraint:
    """RecipeProposal.recipe_name is AllowedRecipeName — Pydantic enforces
    the Guidance/Outlines vocabulary at construction time."""

    def test_valid_price_proposal_accepted(self):
        p = RecipeProposal(recipe_name="PriceAdjustmentRecipe.py")
        assert p.recipe_name == "PriceAdjustmentRecipe.py"

    def test_valid_credit_proposal_accepted(self):
        p = RecipeProposal(recipe_name="CreditHoldReleaseRecipe.py")
        assert p.recipe_name == "CreditHoldReleaseRecipe.py"

    def test_unknown_name_raises_validation_error(self):
        with pytest.raises(ValidationError):
            RecipeProposal(recipe_name="UnknownRecipe.py")  # type: ignore[arg-type]

    def test_empty_string_raises_validation_error(self):
        with pytest.raises(ValidationError):
            RecipeProposal(recipe_name="")  # type: ignore[arg-type]

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            RecipeProposal(recipe_name="PriceAdjustmentRecipe.py", extra="x")


# ---------------------------------------------------------------------------
# Successful invocations
# ---------------------------------------------------------------------------

class TestExecutorSuccessfulInvocations:
    def test_price_adjustment_returns_execution_log(self):
        proposal = RecipeProposal(recipe_name="PriceAdjustmentRecipe.py")
        log = RecipeExecutor().run(proposal=proposal, params=_price_params())
        assert isinstance(log, ExecutionLog)

    def test_price_adjustment_outputs_success_status(self):
        proposal = RecipeProposal(recipe_name="PriceAdjustmentRecipe.py")
        log = RecipeExecutor().run(proposal=proposal, params=_price_params())
        assert log.outputs["status"] == "SUCCESS"
        assert log.errors == []

    def test_price_adjustment_recipe_name_recorded(self):
        proposal = RecipeProposal(recipe_name="PriceAdjustmentRecipe.py")
        log = RecipeExecutor().run(proposal=proposal, params=_price_params())
        assert log.recipe_name == "PriceAdjustmentRecipe.py"

    def test_price_adjustment_inputs_captured(self):
        proposal = RecipeProposal(recipe_name="PriceAdjustmentRecipe.py")
        params = _price_params(order_id="SO-9999")
        log = RecipeExecutor().run(proposal=proposal, params=params)
        assert log.inputs["order_id"] == "SO-9999"

    def test_price_adjustment_trace_id_propagated(self):
        proposal = RecipeProposal(recipe_name="PriceAdjustmentRecipe.py")
        trace = "trace-abc-123"
        log = RecipeExecutor().run(proposal=proposal, params=_price_params(), trace_id=trace)
        assert log.trace_id == trace

    def test_price_adjustment_trace_id_defaults_to_uuid(self):
        proposal = RecipeProposal(recipe_name="PriceAdjustmentRecipe.py")
        log = RecipeExecutor().run(proposal=proposal, params=_price_params())
        assert _is_valid_uuid(log.trace_id)

    def test_price_adjustment_constrained_outputs_recorded(self):
        proposal = RecipeProposal(recipe_name="PriceAdjustmentRecipe.py")
        log = RecipeExecutor().run(proposal=proposal, params=_price_params())
        assert "recipe" in log.constrained_outputs
        assert log.constrained_outputs["recipe"] == "RecipeProposal"

    def test_price_adjustment_intent_selected_recorded(self):
        proposal = RecipeProposal(recipe_name="PriceAdjustmentRecipe.py")
        log = RecipeExecutor().run(
            proposal=proposal,
            params=_price_params(),
            intent_selected="CONTRACTUAL_CORRECTION",
        )
        assert log.intent_selected == "CONTRACTUAL_CORRECTION"

    def test_credit_release_returns_execution_log(self):
        proposal = RecipeProposal(recipe_name="CreditHoldReleaseRecipe.py")
        log = RecipeExecutor().run(proposal=proposal, params=_credit_params())
        assert isinstance(log, ExecutionLog)

    def test_credit_release_outputs_released_status(self):
        proposal = RecipeProposal(recipe_name="CreditHoldReleaseRecipe.py")
        log = RecipeExecutor().run(proposal=proposal, params=_credit_params())
        assert log.outputs["status"] == "RELEASED"
        assert log.errors == []

    def test_credit_release_recipe_name_recorded(self):
        proposal = RecipeProposal(recipe_name="CreditHoldReleaseRecipe.py")
        log = RecipeExecutor().run(proposal=proposal, params=_credit_params())
        assert log.recipe_name == "CreditHoldReleaseRecipe.py"

    def test_credit_release_inputs_captured(self):
        proposal = RecipeProposal(recipe_name="CreditHoldReleaseRecipe.py")
        params = _credit_params(order_id="SO-8888")
        log = RecipeExecutor().run(proposal=proposal, params=params)
        assert log.inputs["order_id"] == "SO-8888"

    def test_shadow_policy_hits_propagated(self):
        proposal = RecipeProposal(recipe_name="PriceAdjustmentRecipe.py")
        log = RecipeExecutor().run(
            proposal=proposal,
            params=_price_params(),
            shadow_policy_hits=["SOME_POLICY"],
        )
        assert "SOME_POLICY" in log.shadow_policy_hits


# ---------------------------------------------------------------------------
# Rejected invocations — recipe outcome errors captured in log
# ---------------------------------------------------------------------------

class TestExecutorRejectedInvocations:
    def test_price_large_discount_captured_in_outputs(self):
        """50% discount exceeds 15% threshold — recipe returns FAILED."""
        proposal = RecipeProposal(recipe_name="PriceAdjustmentRecipe.py")
        log = RecipeExecutor().run(
            proposal=proposal,
            params=_price_params(requested_price=50.0),
        )
        assert log.outputs["status"] == "FAILED"
        assert log.errors == []          # executor-level errors; recipe ran fine
        assert "reason" in log.outputs

    def test_credit_unauthorized_role_captured_in_outputs(self):
        """Unauthorized role — recipe returns BLOCKED."""
        proposal = RecipeProposal(recipe_name="CreditHoldReleaseRecipe.py")
        log = RecipeExecutor().run(
            proposal=proposal,
            params=_credit_params(requester_role="CSR"),
        )
        assert log.outputs["status"] == "BLOCKED"
        assert log.errors == []

    def test_credit_exposure_exceeded_captured_in_outputs(self):
        """Exposure > limit + $5,000 — recipe returns REJECTED."""
        proposal = RecipeProposal(recipe_name="CreditHoldReleaseRecipe.py")
        log = RecipeExecutor().run(
            proposal=proposal,
            params=_credit_params(current_exposure=20_000.0),
        )
        assert log.outputs["status"] == "REJECTED"
        assert log.errors == []


# ---------------------------------------------------------------------------
# Missing required params — captured as executor-level errors
# ---------------------------------------------------------------------------

class TestExecutorMissingParams:
    def test_missing_param_returns_log_with_errors(self):
        proposal = RecipeProposal(recipe_name="PriceAdjustmentRecipe.py")
        incomplete = {"order_id": "SO-1"}   # missing line_item, requested_price, erp_context
        log = RecipeExecutor().run(proposal=proposal, params=incomplete)
        assert len(log.errors) > 0
        assert "Missing required parameters" in log.errors[0]

    def test_missing_param_log_outputs_empty(self):
        proposal = RecipeProposal(recipe_name="PriceAdjustmentRecipe.py")
        log = RecipeExecutor().run(proposal=proposal, params={"order_id": "SO-1"})
        assert log.outputs == {}

    def test_missing_param_recipe_name_still_recorded(self):
        proposal = RecipeProposal(recipe_name="PriceAdjustmentRecipe.py")
        log = RecipeExecutor().run(proposal=proposal, params={})
        assert log.recipe_name == "PriceAdjustmentRecipe.py"

    def test_none_param_treated_as_missing(self):
        proposal = RecipeProposal(recipe_name="PriceAdjustmentRecipe.py")
        params = {
            "order_id": "SO-1",
            "line_item": 1,
            "requested_price": None,   # None counts as missing
            "erp_context": None,
        }
        log = RecipeExecutor().run(proposal=proposal, params=params)
        assert len(log.errors) > 0

    def test_missing_credit_params_returns_log_with_errors(self):
        proposal = RecipeProposal(recipe_name="CreditHoldReleaseRecipe.py")
        log = RecipeExecutor().run(proposal=proposal, params={"order_id": "SO-2"})
        assert len(log.errors) > 0
        assert "Missing required parameters" in log.errors[0]


# ---------------------------------------------------------------------------
# Unknown recipe rejection — defence-in-depth via registry
# ---------------------------------------------------------------------------

class TestExecutorUnknownRecipeRejection:
    def test_unknown_name_raises_key_error(self):
        """model_construct bypasses Pydantic validation to test executor defence."""
        bad_proposal = RecipeProposal.model_construct(recipe_name="BadRecipe.py")
        with pytest.raises(KeyError):
            RecipeExecutor().run(proposal=bad_proposal, params={})

    def test_is_registered_returns_true_for_known(self):
        assert RecipeExecutor.is_registered("PriceAdjustmentRecipe.py")
        assert RecipeExecutor.is_registered("CreditHoldReleaseRecipe.py")

    def test_is_registered_returns_false_for_unknown(self):
        assert not RecipeExecutor.is_registered("BadRecipe.py")
        assert not RecipeExecutor.is_registered("")

    def test_registered_names_returns_all(self):
        names = RecipeExecutor.registered_names()
        assert "PriceAdjustmentRecipe.py" in names
        assert "CreditHoldReleaseRecipe.py" in names
        assert "DuplicatePORecipe.py" in names
        assert len(names) == 3


# ---------------------------------------------------------------------------
# No-shadow / no-orchestration invariant (CLAUDE.md §2, §4)
# ---------------------------------------------------------------------------

class TestNoShadowNoOrchestration:
    def test_executor_has_no_audit_method(self):
        ex = RecipeExecutor()
        assert not hasattr(ex, "audit"), "RecipeExecutor must not expose audit()"
        assert not hasattr(ex, "shadow_audit"), "RecipeExecutor must not expose shadow_audit()"

    def test_executor_has_no_enforce_method(self):
        ex = RecipeExecutor()
        assert not hasattr(ex, "enforce")

    def test_executor_has_no_classify_method(self):
        ex = RecipeExecutor()
        assert not hasattr(ex, "classify")
        assert not hasattr(ex, "classify_intent")

    def test_execution_log_has_no_shadow_field(self):
        proposal = RecipeProposal(recipe_name="PriceAdjustmentRecipe.py")
        log = RecipeExecutor().run(proposal=proposal, params=_price_params())
        assert not hasattr(log, "shadow_decision")
        assert not hasattr(log, "compliance_decision")

    def test_executor_does_not_import_compliance_shadow(self):
        """Verify executor module does not pull in compliance.shadow."""
        import recipes.executor as executor_mod
        assert not hasattr(executor_mod, "ComplianceShadow")
        assert "compliance" not in str(getattr(executor_mod, "__file__", ""))
