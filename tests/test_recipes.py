from __future__ import annotations

# Phase 3 — Recipe function tests (immutable units)
#
# Recipes are pure, immutable Python functions.  These tests call them
# directly to verify their deterministic behaviour without going through
# the executor or any orchestration layer.
#
# Covered:
#   PriceAdjustmentRecipe  — success, discount-too-large rejection
#   CreditHoldReleaseRecipe — success, unauthorized role, exposure limit

from recipes.PriceAdjustmentRecipe import execute_price_correction
from recipes.CreditHoldReleaseRecipe import release_credit_hold
from recipes.DuplicatePORecipe import detect_duplicate_po


# ---------------------------------------------------------------------------
# PriceAdjustmentRecipe
# ---------------------------------------------------------------------------

class TestPriceAdjustmentRecipe:
    def _ctx(self, base: float = 100.0, threshold: float = 0.15, condition_type: str = "YK07"):
        return {"base_price": base, "max_discount_allowed": threshold, "condition_type": condition_type}

    # -- success path --------------------------------------------------------

    def test_small_discount_succeeds(self):
        result = execute_price_correction(
            order_id="SO-1001", line_item=1,
            requested_price=90.0,
            erp_context=self._ctx(),
        )
        assert result["status"] == "SUCCESS"

    def test_success_applies_condition_yk07(self):
        result = execute_price_correction(
            order_id="SO-1001", line_item=1,
            requested_price=90.0,
            erp_context=self._ctx(),
        )
        assert result["applied_condition"] == "YK07"

    def test_success_returns_new_net_price(self):
        result = execute_price_correction(
            order_id="SO-1001", line_item=1,
            requested_price=90.0,
            erp_context=self._ctx(),
        )
        assert result["new_net_price"] == 90.0

    def test_success_payload_contains_order_id(self):
        result = execute_price_correction(
            order_id="SO-1001", line_item=2,
            requested_price=92.0,
            erp_context=self._ctx(),
        )
        assert result["payload"]["OrderID"] == "SO-1001"
        assert result["payload"]["Item"] == 2

    def test_exact_threshold_boundary_succeeds(self):
        """Exactly 15 % discount must succeed (not greater than)."""
        result = execute_price_correction(
            order_id="SO-1001", line_item=1,
            requested_price=85.0,          # exactly 15% off 100
            erp_context=self._ctx(),
        )
        assert result["status"] == "SUCCESS"

    # -- rejection path ------------------------------------------------------

    def test_large_discount_rejected(self):
        result = execute_price_correction(
            order_id="SO-1", line_item=1,
            requested_price=50.0,
            erp_context=self._ctx(),
        )
        assert result["status"] == "FAILED"

    def test_large_discount_includes_reason(self):
        result = execute_price_correction(
            order_id="SO-1", line_item=1,
            requested_price=50.0,
            erp_context=self._ctx(),
        )
        assert "reason" in result
        assert "15%" in result["reason"] or "threshold" in result["reason"]

    def test_zero_price_rejected(self):
        result = execute_price_correction(
            order_id="SO-1", line_item=1,
            requested_price=0.0,
            erp_context=self._ctx(),
        )
        assert result["status"] == "FAILED"

    def test_custom_threshold_respected(self):
        """A tighter threshold (5%) should reject a 10% discount."""
        result = execute_price_correction(
            order_id="SO-1", line_item=1,
            requested_price=90.0,          # 10% off
            erp_context=self._ctx(threshold=0.05),
        )
        assert result["status"] == "FAILED"


# ---------------------------------------------------------------------------
# CreditHoldReleaseRecipe
# ---------------------------------------------------------------------------

class TestCreditHoldReleaseRecipe:

    _ROLES = ("ORDER_MANAGER", "FINANCE_DIRECTOR")
    _TOLERANCE = 5_000.0

    # -- success path --------------------------------------------------------

    def test_order_manager_within_limit_succeeds(self):
        result = release_credit_hold(
            order_id="SO-2001",
            requester_role="ORDER_MANAGER",
            credit_limit=10_000.0,
            current_exposure=9_000.0,
            authorized_roles=self._ROLES,
            exposure_tolerance=self._TOLERANCE,
        )
        assert result["status"] == "RELEASED"

    def test_finance_director_within_limit_succeeds(self):
        result = release_credit_hold(
            order_id="SO-2002",
            requester_role="FINANCE_DIRECTOR",
            credit_limit=50_000.0,
            current_exposure=45_000.0,
            authorized_roles=self._ROLES,
            exposure_tolerance=self._TOLERANCE,
        )
        assert result["status"] == "RELEASED"

    def test_success_returns_order_id(self):
        result = release_credit_hold(
            order_id="SO-2001",
            requester_role="ORDER_MANAGER",
            credit_limit=10_000.0,
            current_exposure=9_000.0,
            authorized_roles=self._ROLES,
            exposure_tolerance=self._TOLERANCE,
        )
        assert result["order_id"] == "SO-2001"

    def test_success_workflow_is_auto_approved(self):
        result = release_credit_hold(
            order_id="SO-2001",
            requester_role="ORDER_MANAGER",
            credit_limit=10_000.0,
            current_exposure=9_000.0,
            authorized_roles=self._ROLES,
            exposure_tolerance=self._TOLERANCE,
        )
        assert result["workflow"] == "AUTO_APPROVED"

    def test_exposure_exactly_5000_over_limit_succeeds(self):
        """Exposure exactly at limit + $5,000 boundary must NOT be rejected."""
        result = release_credit_hold(
            order_id="SO-2003",
            requester_role="ORDER_MANAGER",
            credit_limit=10_000.0,
            current_exposure=15_000.0,   # exactly $5,000 over (not >5000)
            authorized_roles=self._ROLES,
            exposure_tolerance=self._TOLERANCE,
        )
        assert result["status"] == "RELEASED"

    # -- unauthorized role path ----------------------------------------------

    def test_unauthorized_role_blocked(self):
        result = release_credit_hold(
            order_id="SO-2", requester_role="CSR",
            credit_limit=10_000.0, current_exposure=9_500.0,
            authorized_roles=self._ROLES,
            exposure_tolerance=self._TOLERANCE,
        )
        assert result["status"] == "BLOCKED"

    def test_unauthorized_role_includes_reason(self):
        result = release_credit_hold(
            order_id="SO-2", requester_role="CSR",
            credit_limit=10_000.0, current_exposure=9_500.0,
            authorized_roles=self._ROLES,
            exposure_tolerance=self._TOLERANCE,
        )
        assert "reason" in result

    def test_unknown_role_blocked(self):
        result = release_credit_hold(
            order_id="SO-2", requester_role="ADMIN",
            credit_limit=10_000.0, current_exposure=9_500.0,
            authorized_roles=self._ROLES,
            exposure_tolerance=self._TOLERANCE,
        )
        assert result["status"] == "BLOCKED"

    # -- exposure limit path -------------------------------------------------

    def test_exposure_exceeds_limit_by_more_than_5000_rejected(self):
        result = release_credit_hold(
            order_id="SO-2004",
            requester_role="ORDER_MANAGER",
            credit_limit=10_000.0,
            current_exposure=15_001.0,   # $5,001 over limit
            authorized_roles=self._ROLES,
            exposure_tolerance=self._TOLERANCE,
        )
        assert result["status"] == "REJECTED"

    def test_rejection_includes_reason(self):
        result = release_credit_hold(
            order_id="SO-2004",
            requester_role="ORDER_MANAGER",
            credit_limit=10_000.0,
            current_exposure=20_000.0,
            authorized_roles=self._ROLES,
            exposure_tolerance=self._TOLERANCE,
        )
        assert "reason" in result
        assert "5,000" in result["reason"] or "5000" in result["reason"]


# ---------------------------------------------------------------------------
# DuplicatePORecipe
# ---------------------------------------------------------------------------

class TestDuplicatePORecipe:
    """Tests for detect_duplicate_po — pure scoring and classification."""

    def _perfect_signals(self) -> dict:
        """All signals score 1.0 → composite = 1.0 → AUTO_BLOCK."""
        return {k: 1.0 for k in (
            "po_number", "customer_id", "line_items", "amount",
            "timestamp", "ship_to", "channel", "delivery_date",
        )}

    def _no_signals(self) -> dict:
        """All signals score 0.0 → composite = 0.0 → PASS."""
        return {k: 0.0 for k in (
            "po_number", "customer_id", "line_items", "amount",
            "timestamp", "ship_to", "channel", "delivery_date",
        )}

    # -- AUTO_BLOCK path (score >= 0.90) ------------------------------------

    def test_perfect_match_is_blocked(self):
        result = detect_duplicate_po("PO-001", "cust-1", self._perfect_signals())
        assert result["status"] == "BLOCKED"

    def test_perfect_match_classification_is_auto_block(self):
        result = detect_duplicate_po("PO-001", "cust-1", self._perfect_signals())
        assert result["classification"] == "AUTO_BLOCK"

    def test_perfect_match_recommends_block_and_notify(self):
        result = detect_duplicate_po("PO-001", "cust-1", self._perfect_signals())
        assert result["recommended_action"] == "BLOCK_AND_NOTIFY"

    def test_composite_score_equals_one_for_all_perfect_signals(self):
        result = detect_duplicate_po("PO-001", "cust-1", self._perfect_signals())
        assert abs(result["composite_score"] - 1.0) < 1e-6

    # -- REVIEW_REQUIRED path (0.70 <= score < 0.90) -----------------------

    def test_high_confidence_partial_match_is_review_required(self):
        # po_number(0.30) + customer_id(0.15) + line_items(0.20) = 0.65 — not enough.
        # Add amount(0.10) = 0.75 → REVIEW_REQUIRED.
        signals = self._no_signals()
        signals.update({"po_number": 1.0, "customer_id": 1.0, "line_items": 1.0, "amount": 1.0})
        result = detect_duplicate_po("PO-002", "cust-2", signals)
        assert result["status"] == "REVIEW_REQUIRED"
        assert result["recommended_action"] == "ESCALATE"  # REVIEW_REQUIRED default

    # -- SOFT_FLAG path (0.50 <= score < 0.70) -----------------------------

    def test_moderate_match_is_soft_flag(self):
        # po_number(0.30) + customer_id(0.15) + line_items(0.20) = 0.65 → SOFT_FLAG.
        signals = self._no_signals()
        signals.update({"po_number": 1.0, "customer_id": 1.0, "line_items": 1.0})
        result = detect_duplicate_po("PO-003", "cust-3", signals)
        assert result["status"] == "SOFT_FLAG"
        assert result["recommended_action"] == "REQUEST_BUYER_CONFIRMATION"

    # -- PASS path (score < 0.50) ------------------------------------------

    def test_no_signals_is_pass(self):
        result = detect_duplicate_po("PO-004", "cust-4", self._no_signals())
        assert result["status"] == "PASS"
        assert result["recommended_action"] == "ALLOW_BOTH"

    def test_composite_score_zero_for_no_signals(self):
        result = detect_duplicate_po("PO-004", "cust-4", self._no_signals())
        assert result["composite_score"] == 0.0

    # -- Threshold boundary (score == 0.90 → AUTO_BLOCK) ------------------

    def test_score_at_auto_block_boundary_is_blocked(self):
        """Score exactly 0.90 must classify as AUTO_BLOCK (closed lower bound)."""
        # po_number(0.30) + customer_id(0.15) + line_items(0.20) +
        # amount(0.10) + timestamp(0.10) + ship_to(0.05) = 0.90
        signals = self._no_signals()
        signals.update({
            "po_number": 1.0, "customer_id": 1.0, "line_items": 1.0,
            "amount": 1.0, "timestamp": 1.0, "ship_to": 1.0,
        })
        result = detect_duplicate_po("PO-005", "cust-5", signals)
        assert result["status"] == "BLOCKED"

    # -- Output structure --------------------------------------------------

    def test_result_contains_signal_breakdown(self):
        result = detect_duplicate_po("PO-006", "cust-6", self._perfect_signals())
        assert "signal_breakdown" in result
        assert len(result["signal_breakdown"]) == 8

    def test_result_echoes_incoming_po_number(self):
        result = detect_duplicate_po("PO-007", "cust-7", self._perfect_signals())
        assert result["incoming_po_number"] == "PO-007"

    def test_result_echoes_customer_id(self):
        result = detect_duplicate_po("PO-008", "cust-8", self._perfect_signals())
        assert result["customer_id"] == "cust-8"

    def test_missing_signals_default_to_zero(self):
        """Calling with an empty signal dict should not raise and must PASS."""
        result = detect_duplicate_po("PO-009", "cust-9", {})
        assert result["status"] == "PASS"
        assert result["composite_score"] == 0.0


# ---------------------------------------------------------------------------
# Architectural invariant: recipes must NOT import from contracts.policy
# ---------------------------------------------------------------------------

class TestRecipePolicyDecoupling:
    """Guard against regression of the recipe-policy decoupling invariant.

    Recipes are immutable execution logic.  All thresholds must be injected
    by the orchestration layer — recipes must never import from the policy
    module directly.
    """

    @staticmethod
    def _has_policy_import(module) -> bool:
        """Check whether *module* has an actual import of contracts.policy."""
        import ast, inspect, textwrap
        source = inspect.getsource(module)
        tree = ast.parse(textwrap.dedent(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "contracts.policy" in node.module:
                return True
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "contracts.policy" in alias.name:
                        return True
        return False

    def test_price_adjustment_no_policy_import(self):
        import inspect
        mod = inspect.getmodule(execute_price_correction)
        assert not self._has_policy_import(mod), (
            "PriceAdjustmentRecipe must not import from contracts.policy"
        )

    def test_credit_hold_release_no_policy_import(self):
        import inspect
        mod = inspect.getmodule(release_credit_hold)
        assert not self._has_policy_import(mod), (
            "CreditHoldReleaseRecipe must not import from contracts.policy"
        )

    def test_duplicate_po_no_policy_import(self):
        import inspect
        mod = inspect.getmodule(detect_duplicate_po)
        assert not self._has_policy_import(mod), (
            "DuplicatePORecipe must not import from contracts.policy"
        )
