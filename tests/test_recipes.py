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


class TestDuplicatePODecisionTree:
    """Tests for the resolution decision tree (spec §3.2).

    Each test covers one leaf node of the decision tree, verifying that
    resolution context (original_fulfilled, has_revision_indicator,
    line_items_identical) refines the recommended_action within a
    classification tier.
    """

    def _perfect_signals(self) -> dict:
        return {k: 1.0 for k in (
            "po_number", "customer_id", "line_items", "amount",
            "timestamp", "ship_to", "channel", "delivery_date",
        )}

    def _review_signals(self) -> dict:
        """Composite ~0.75 → REVIEW_REQUIRED."""
        signals = {k: 0.0 for k in (
            "po_number", "customer_id", "line_items", "amount",
            "timestamp", "ship_to", "channel", "delivery_date",
        )}
        signals.update({"po_number": 1.0, "customer_id": 1.0, "line_items": 1.0, "amount": 1.0})
        return signals

    # -- AUTO_BLOCK: identical lines + not fulfilled → BLOCK_AND_NOTIFY ------

    def test_auto_block_identical_not_fulfilled_blocks(self):
        result = detect_duplicate_po(
            "PO-DT01", "cust-1", self._perfect_signals(),
            original_fulfilled=False, has_revision_indicator=False, line_items_identical=True,
        )
        assert result["classification"] == "AUTO_BLOCK"
        assert result["recommended_action"] == "BLOCK_AND_NOTIFY"

    # -- AUTO_BLOCK: identical lines + fulfilled → ALLOW_BOTH ----------------

    def test_auto_block_identical_fulfilled_allows_both(self):
        result = detect_duplicate_po(
            "PO-DT02", "cust-2", self._perfect_signals(),
            original_fulfilled=True, has_revision_indicator=False, line_items_identical=True,
        )
        assert result["classification"] == "AUTO_BLOCK"
        assert result["recommended_action"] == "ALLOW_BOTH"

    # -- AUTO_BLOCK: revision indicator → SUPERSEDE --------------------------

    def test_auto_block_revision_indicator_supersedes(self):
        result = detect_duplicate_po(
            "PO-DT03", "cust-3", self._perfect_signals(),
            original_fulfilled=False, has_revision_indicator=True, line_items_identical=False,
        )
        assert result["classification"] == "AUTO_BLOCK"
        assert result["recommended_action"] == "SUPERSEDE"

    # -- AUTO_BLOCK: lines differ + no revision → MERGE ---------------------

    def test_auto_block_lines_differ_no_revision_merges(self):
        result = detect_duplicate_po(
            "PO-DT04", "cust-4", self._perfect_signals(),
            original_fulfilled=False, has_revision_indicator=False, line_items_identical=False,
        )
        assert result["classification"] == "AUTO_BLOCK"
        assert result["recommended_action"] == "MERGE"

    # -- REVIEW_REQUIRED: revision indicator → SUPERSEDE --------------------

    def test_review_required_revision_indicator_supersedes(self):
        result = detect_duplicate_po(
            "PO-DT05", "cust-5", self._review_signals(),
            original_fulfilled=False, has_revision_indicator=True, line_items_identical=False,
        )
        assert result["classification"] == "REVIEW_REQUIRED"
        assert result["recommended_action"] == "SUPERSEDE"

    # -- REVIEW_REQUIRED: identical lines → ESCALATE -----------------------

    def test_review_required_identical_lines_escalates(self):
        result = detect_duplicate_po(
            "PO-DT06", "cust-6", self._review_signals(),
            original_fulfilled=False, has_revision_indicator=False, line_items_identical=True,
        )
        assert result["classification"] == "REVIEW_REQUIRED"
        assert result["recommended_action"] == "ESCALATE"

    # -- REVIEW_REQUIRED: lines differ → REQUEST_BUYER_CONFIRMATION --------

    def test_review_required_lines_differ_requests_confirmation(self):
        result = detect_duplicate_po(
            "PO-DT07", "cust-7", self._review_signals(),
            original_fulfilled=False, has_revision_indicator=False, line_items_identical=False,
        )
        assert result["classification"] == "REVIEW_REQUIRED"
        assert result["recommended_action"] == "REQUEST_BUYER_CONFIRMATION"

    # -- SOFT_FLAG / PASS: context does not change defaults ----------------

    def test_soft_flag_with_context_uses_default(self):
        """SOFT_FLAG always returns REQUEST_BUYER_CONFIRMATION regardless of context."""
        signals = {k: 0.0 for k in (
            "po_number", "customer_id", "line_items", "amount",
            "timestamp", "ship_to", "channel", "delivery_date",
        )}
        signals.update({"po_number": 1.0, "customer_id": 1.0, "line_items": 1.0})  # 0.65
        result = detect_duplicate_po(
            "PO-DT08", "cust-8", signals,
            original_fulfilled=True, has_revision_indicator=False, line_items_identical=True,
        )
        assert result["classification"] == "SOFT_FLAG"
        assert result["recommended_action"] == "REQUEST_BUYER_CONFIRMATION"

    def test_pass_with_context_uses_default(self):
        """PASS always returns ALLOW_BOTH regardless of context."""
        result = detect_duplicate_po(
            "PO-DT09", "cust-9", {k: 0.0 for k in (
            "po_number", "customer_id", "line_items", "amount",
            "timestamp", "ship_to", "channel", "delivery_date",
        )},
            original_fulfilled=False, has_revision_indicator=True, line_items_identical=False,
        )
        assert result["classification"] == "PASS"
        assert result["recommended_action"] == "ALLOW_BOTH"

    # -- No context → falls back to default --------------------------------

    def test_auto_block_no_context_uses_default(self):
        result = detect_duplicate_po("PO-DT10", "cust-10", self._perfect_signals())
        assert result["classification"] == "AUTO_BLOCK"
        assert result["recommended_action"] == "BLOCK_AND_NOTIFY"

    def test_review_required_no_context_uses_default(self):
        result = detect_duplicate_po("PO-DT11", "cust-11", self._review_signals())
        assert result["classification"] == "REVIEW_REQUIRED"
        assert result["recommended_action"] == "ESCALATE"


class TestDuplicatePOAutonomyLevel:
    """Tests for autonomy_level field in recipe output."""

    _ALL_SIGNALS = {
        "po_number": 1.0, "customer_id": 1.0, "line_items": 1.0,
        "amount": 1.0, "timestamp": 1.0, "ship_to": 1.0,
        "channel": 1.0, "delivery_date": 1.0,
    }
    _AUTONOMY = {
        "BLOCK_AND_NOTIFY": "L3", "MERGE": "L2", "SUPERSEDE": "L2",
        "ALLOW_BOTH": "L3", "ESCALATE": "L1", "REQUEST_BUYER_CONFIRMATION": "L2",
    }

    def test_autonomy_level_present_when_mapping_injected(self):
        result = detect_duplicate_po(
            "PO-AL01", "cust-1", self._ALL_SIGNALS, autonomy_levels=self._AUTONOMY,
        )
        assert "autonomy_level" in result

    def test_autonomy_level_none_when_no_mapping(self):
        result = detect_duplicate_po("PO-AL02", "cust-2", self._ALL_SIGNALS)
        assert result["autonomy_level"] is None

    def test_block_and_notify_is_l3(self):
        result = detect_duplicate_po(
            "PO-AL03", "cust-3", self._ALL_SIGNALS,
            original_fulfilled=False, has_revision_indicator=False,
            line_items_identical=True, autonomy_levels=self._AUTONOMY,
        )
        assert result["recommended_action"] == "BLOCK_AND_NOTIFY"
        assert result["autonomy_level"] == "L3"

    def test_merge_is_l2(self):
        result = detect_duplicate_po(
            "PO-AL04", "cust-4", self._ALL_SIGNALS,
            original_fulfilled=False, has_revision_indicator=False,
            line_items_identical=False, autonomy_levels=self._AUTONOMY,
        )
        assert result["recommended_action"] == "MERGE"
        assert result["autonomy_level"] == "L2"

    def test_escalate_is_l1(self):
        signals = {k: 0.0 for k in self._ALL_SIGNALS}
        signals.update({"po_number": 1.0, "customer_id": 1.0, "line_items": 1.0, "amount": 1.0})
        result = detect_duplicate_po(
            "PO-AL05", "cust-5", signals,
            original_fulfilled=False, has_revision_indicator=False,
            line_items_identical=True, autonomy_levels=self._AUTONOMY,
        )
        assert result["recommended_action"] == "ESCALATE"
        assert result["autonomy_level"] == "L1"


class TestDuplicatePONotificationTemplate:
    """Tests for notification_template in recipe output (spec §7.3)."""

    _ALL_SIGNALS = {
        "po_number": 1.0, "customer_id": 1.0, "line_items": 1.0,
        "amount": 1.0, "timestamp": 1.0, "ship_to": 1.0,
        "channel": 1.0, "delivery_date": 1.0,
    }

    def test_block_and_notify_uses_blocked_template(self):
        result = detect_duplicate_po(
            "PO-NT01", "cust-1", self._ALL_SIGNALS,
            original_fulfilled=False, has_revision_indicator=False, line_items_identical=True,
        )
        assert result["recommended_action"] == "BLOCK_AND_NOTIFY"
        assert result["notification_template"] == "duplicate_po_blocked"

    def test_merge_uses_amended_template(self):
        result = detect_duplicate_po(
            "PO-NT02", "cust-2", self._ALL_SIGNALS,
            original_fulfilled=False, has_revision_indicator=False, line_items_identical=False,
        )
        assert result["recommended_action"] == "MERGE"
        assert result["notification_template"] == "duplicate_po_amended"

    def test_supersede_uses_amended_template(self):
        result = detect_duplicate_po(
            "PO-NT03", "cust-3", self._ALL_SIGNALS,
            original_fulfilled=False, has_revision_indicator=True, line_items_identical=False,
        )
        assert result["recommended_action"] == "SUPERSEDE"
        assert result["notification_template"] == "duplicate_po_amended"

    def test_request_buyer_confirmation_uses_inquiry_template(self):
        signals = {k: 0.0 for k in self._ALL_SIGNALS}
        signals.update({"po_number": 1.0, "customer_id": 1.0, "line_items": 1.0})  # 0.65 → SOFT_FLAG
        result = detect_duplicate_po("PO-NT04", "cust-4", signals)
        assert result["recommended_action"] == "REQUEST_BUYER_CONFIRMATION"
        assert result["notification_template"] == "duplicate_po_inquiry"

    def test_allow_both_has_no_notification(self):
        result = detect_duplicate_po(
            "PO-NT05", "cust-5", self._ALL_SIGNALS,
            original_fulfilled=True, has_revision_indicator=False, line_items_identical=True,
        )
        assert result["recommended_action"] == "ALLOW_BOTH"
        assert result["notification_template"] is None

    def test_escalate_has_no_notification(self):
        signals = {k: 0.0 for k in self._ALL_SIGNALS}
        signals.update({"po_number": 1.0, "customer_id": 1.0, "line_items": 1.0, "amount": 1.0})
        result = detect_duplicate_po(
            "PO-NT06", "cust-6", signals,
            original_fulfilled=False, has_revision_indicator=False, line_items_identical=True,
        )
        assert result["recommended_action"] == "ESCALATE"
        assert result["notification_template"] is None


# ---------------------------------------------------------------------------
# EC04 — Exact duplicate PO resend (Costco-style)
#
# Scenario: a retailer resends a PO because the first email bounced.
# All signals should score high (same PO number, customer, line items, amount,
# ship-to, channel, delivery date).  System must detect as duplicate and
# select the correct resolution action based on fulfillment/revision context.
# ---------------------------------------------------------------------------

class TestDuplicatePOResend:
    """EC04-style tests: exact PO resend triggers duplicate detection."""

    def _resend_signals(self) -> dict:
        """Resend: same PO, same customer, same lines, same amount,
        same ship-to, same channel, same delivery date. Only timestamp
        differs (resend is later)."""
        return {
            "po_number":     1.0,
            "customer_id":   1.0,
            "line_items":    1.0,
            "amount":        1.0,
            "timestamp":     0.3,   # different send time
            "ship_to":       1.0,
            "channel":       1.0,
            "delivery_date": 1.0,
        }

    def test_resend_scores_above_auto_block(self):
        """Exact resend (timestamp differs) composite = 0.92 >= 0.90."""
        result = detect_duplicate_po("PO-88424", "COSTCO", self._resend_signals())
        assert result["composite_score"] >= 0.90
        assert result["classification"] == "AUTO_BLOCK"

    def test_resend_not_fulfilled_blocks_and_notifies(self):
        """Original not yet fulfilled — true duplicate, block it."""
        result = detect_duplicate_po(
            "PO-88424", "COSTCO", self._resend_signals(),
            original_fulfilled=False, has_revision_indicator=False,
            line_items_identical=True,
        )
        assert result["recommended_action"] == "BLOCK_AND_NOTIFY"
        assert result["notification_template"] == "duplicate_po_blocked"

    def test_resend_already_fulfilled_allows_both(self):
        """Original already shipped — likely a reorder, allow both."""
        result = detect_duplicate_po(
            "PO-88424", "COSTCO", self._resend_signals(),
            original_fulfilled=True, has_revision_indicator=False,
            line_items_identical=True,
        )
        assert result["recommended_action"] == "ALLOW_BOTH"
        assert result["notification_template"] is None

    def test_resend_with_revision_indicator_supersedes(self):
        """Resend includes a revision indicator — supersede original."""
        result = detect_duplicate_po(
            "PO-88424", "COSTCO", self._resend_signals(),
            original_fulfilled=False, has_revision_indicator=True,
            line_items_identical=False,
        )
        assert result["recommended_action"] == "SUPERSEDE"
        assert result["notification_template"] == "duplicate_po_amended"

    def test_resend_with_amended_lines_merges(self):
        """Resend has different line items and no revision flag — merge."""
        result = detect_duplicate_po(
            "PO-88424", "COSTCO", self._resend_signals(),
            original_fulfilled=False, has_revision_indicator=False,
            line_items_identical=False,
        )
        assert result["recommended_action"] == "MERGE"
        assert result["notification_template"] == "duplicate_po_amended"

    def test_resend_echoes_po_number_and_customer(self):
        result = detect_duplicate_po("PO-88424", "COSTCO", self._resend_signals())
        assert result["incoming_po_number"] == "PO-88424"
        assert result["customer_id"] == "COSTCO"

    def test_resend_signal_breakdown_has_all_eight_signals(self):
        result = detect_duplicate_po("PO-88424", "COSTCO", self._resend_signals())
        assert len(result["signal_breakdown"]) == 8

    def test_resend_autonomy_l3_for_block_and_notify(self):
        """BLOCK_AND_NOTIFY is L3 (auto-execute) per policy."""
        autonomy = {
            "BLOCK_AND_NOTIFY": "L3", "MERGE": "L2", "SUPERSEDE": "L2",
            "ALLOW_BOTH": "L3", "ESCALATE": "L1", "REQUEST_BUYER_CONFIRMATION": "L2",
        }
        result = detect_duplicate_po(
            "PO-88424", "COSTCO", self._resend_signals(),
            original_fulfilled=False, has_revision_indicator=False,
            line_items_identical=True, autonomy_levels=autonomy,
        )
        assert result["autonomy_level"] == "L3"


# ---------------------------------------------------------------------------
# EC08 — Multiple POs in single message (Amazon batch-style)
#
# Scenario: a retailer sends a weekly batch containing 3 distinct POs.
# Each PO must be scored independently.  Different POs should get different
# classifications depending on their individual signal scores.
# ---------------------------------------------------------------------------

class TestDuplicatePOMultiplePOBatch:
    """EC08-style tests: batch of POs processed independently."""

    def _batch_po_signals(self) -> list:
        """Three POs from the same batch email, each with different signals."""
        return [
            # PO-AMZ-001: matches an existing PO exactly
            {
                "po_id": "PO-AMZ-001", "customer": "AMAZON",
                "signals": {
                    "po_number": 1.0, "customer_id": 1.0, "line_items": 1.0,
                    "amount": 1.0, "timestamp": 0.8, "ship_to": 1.0,
                    "channel": 1.0, "delivery_date": 1.0,
                },
            },
            # PO-AMZ-002: partial match (different line items, amount)
            {
                "po_id": "PO-AMZ-002", "customer": "AMAZON",
                "signals": {
                    "po_number": 0.8, "customer_id": 1.0, "line_items": 0.4,
                    "amount": 0.3, "timestamp": 0.0, "ship_to": 0.5,
                    "channel": 1.0, "delivery_date": 0.5,
                },
            },
            # PO-AMZ-003: no match in system
            {
                "po_id": "PO-AMZ-003", "customer": "AMAZON",
                "signals": {
                    "po_number": 0.0, "customer_id": 1.0, "line_items": 0.0,
                    "amount": 0.0, "timestamp": 0.0, "ship_to": 0.0,
                    "channel": 1.0, "delivery_date": 0.0,
                },
            },
        ]

    def test_batch_each_po_classified_independently(self):
        """Each PO in a batch gets its own classification."""
        results = [
            detect_duplicate_po(po["po_id"], po["customer"], po["signals"])
            for po in self._batch_po_signals()
        ]
        classifications = [r["classification"] for r in results]
        # PO-AMZ-001 → AUTO_BLOCK, PO-AMZ-002 → SOFT_FLAG, PO-AMZ-003 → PASS
        assert classifications[0] == "AUTO_BLOCK"
        assert classifications[1] == "SOFT_FLAG"
        assert classifications[2] == "PASS"

    def test_batch_po1_exact_match_blocked(self):
        po = self._batch_po_signals()[0]
        result = detect_duplicate_po(po["po_id"], po["customer"], po["signals"])
        assert result["status"] == "BLOCKED"
        assert result["composite_score"] >= 0.90

    def test_batch_po2_partial_match_soft_flag(self):
        po = self._batch_po_signals()[1]
        result = detect_duplicate_po(po["po_id"], po["customer"], po["signals"])
        assert result["status"] == "SOFT_FLAG"
        assert 0.50 <= result["composite_score"] < 0.70

    def test_batch_po3_no_match_pass(self):
        po = self._batch_po_signals()[2]
        result = detect_duplicate_po(po["po_id"], po["customer"], po["signals"])
        assert result["status"] == "PASS"
        assert result["recommended_action"] == "ALLOW_BOTH"

    def test_batch_po_numbers_echoed_correctly(self):
        """Each result echoes back the correct PO number."""
        for po in self._batch_po_signals():
            result = detect_duplicate_po(po["po_id"], po["customer"], po["signals"])
            assert result["incoming_po_number"] == po["po_id"]
            assert result["customer_id"] == po["customer"]


# ---------------------------------------------------------------------------
# Additional Duplicate PO edge cases and threshold boundaries
# ---------------------------------------------------------------------------

class TestDuplicatePOEdgeCases:
    """Additional edge case and boundary tests for Duplicate PO detection."""

    def test_score_at_review_boundary_is_review_required(self):
        """Score exactly 0.70 must classify as REVIEW_REQUIRED (closed lower bound)."""
        # po_number(0.30) + customer_id(0.15) + line_items(0.20) +
        # amount(0.10*0.5=0.05) = 0.70
        signals = {k: 0.0 for k in (
            "po_number", "customer_id", "line_items", "amount",
            "timestamp", "ship_to", "channel", "delivery_date",
        )}
        signals.update({
            "po_number": 1.0, "customer_id": 1.0, "line_items": 1.0, "amount": 0.5,
        })
        result = detect_duplicate_po("PO-EDGE-01", "cust-edge", signals)
        assert abs(result["composite_score"] - 0.70) < 1e-6
        assert result["classification"] == "REVIEW_REQUIRED"

    def test_score_at_soft_flag_boundary_is_soft_flag(self):
        """Score exactly 0.50 must classify as SOFT_FLAG (closed lower bound)."""
        # po_number(0.30) + customer_id(0.15) + amount(0.10*0.5=0.05) = 0.50
        signals = {k: 0.0 for k in (
            "po_number", "customer_id", "line_items", "amount",
            "timestamp", "ship_to", "channel", "delivery_date",
        )}
        signals.update({"po_number": 1.0, "customer_id": 1.0, "amount": 0.5})
        result = detect_duplicate_po("PO-EDGE-02", "cust-edge", signals)
        assert abs(result["composite_score"] - 0.50) < 1e-6
        assert result["classification"] == "SOFT_FLAG"

    def test_score_just_below_soft_flag_is_pass(self):
        """Score 0.45 (below 0.50) must be PASS."""
        # po_number(0.30) + customer_id(0.15) = 0.45
        signals = {k: 0.0 for k in (
            "po_number", "customer_id", "line_items", "amount",
            "timestamp", "ship_to", "channel", "delivery_date",
        )}
        signals.update({"po_number": 1.0, "customer_id": 1.0})
        result = detect_duplicate_po("PO-EDGE-03", "cust-edge", signals)
        assert result["composite_score"] == 0.45
        assert result["classification"] == "PASS"

    def test_score_just_below_review_boundary_is_soft_flag(self):
        """Score 0.65 (below 0.70) must be SOFT_FLAG."""
        signals = {k: 0.0 for k in (
            "po_number", "customer_id", "line_items", "amount",
            "timestamp", "ship_to", "channel", "delivery_date",
        )}
        signals.update({"po_number": 1.0, "customer_id": 1.0, "line_items": 1.0})
        result = detect_duplicate_po("PO-EDGE-04", "cust-edge", signals)
        assert result["composite_score"] == 0.65
        assert result["classification"] == "SOFT_FLAG"

    def test_custom_thresholds_lower_auto_block(self):
        """A stricter auto_block threshold (0.80) blocks more aggressively."""
        signals = {k: 0.0 for k in (
            "po_number", "customer_id", "line_items", "amount",
            "timestamp", "ship_to", "channel", "delivery_date",
        )}
        signals.update({
            "po_number": 1.0, "customer_id": 1.0, "line_items": 1.0,
            "amount": 1.0, "timestamp": 0.5,
        })
        # composite = 0.30 + 0.15 + 0.20 + 0.10 + 0.05 = 0.80
        result = detect_duplicate_po(
            "PO-EDGE-05", "cust-edge", signals,
            threshold_auto_block=0.80,
        )
        assert result["classification"] == "AUTO_BLOCK"

    def test_custom_thresholds_higher_auto_block_keeps_review(self):
        """A relaxed auto_block threshold (0.95) keeps score 0.80 as REVIEW_REQUIRED."""
        signals = {k: 0.0 for k in (
            "po_number", "customer_id", "line_items", "amount",
            "timestamp", "ship_to", "channel", "delivery_date",
        )}
        signals.update({
            "po_number": 1.0, "customer_id": 1.0, "line_items": 1.0,
            "amount": 1.0, "timestamp": 0.5,
        })
        result = detect_duplicate_po(
            "PO-EDGE-06", "cust-edge", signals,
            threshold_auto_block=0.95,
        )
        assert result["classification"] == "REVIEW_REQUIRED"

    def test_only_po_number_signal_present(self):
        """Only po_number matches — score = 0.30, well below SOFT_FLAG."""
        signals = {"po_number": 1.0}
        result = detect_duplicate_po("PO-EDGE-07", "cust-edge", signals)
        assert result["composite_score"] == 0.30
        assert result["classification"] == "PASS"

    def test_fractional_signal_scores_aggregate_correctly(self):
        """Verify weighted aggregation with fractional scores."""
        signals = {
            "po_number": 0.5, "customer_id": 0.5, "line_items": 0.5,
            "amount": 0.5, "timestamp": 0.5, "ship_to": 0.5,
            "channel": 0.5, "delivery_date": 0.5,
        }
        result = detect_duplicate_po("PO-EDGE-08", "cust-edge", signals)
        # All weights sum to 1.0; each signal is 0.5 → composite = 0.50
        assert abs(result["composite_score"] - 0.50) < 1e-6
        assert result["classification"] == "SOFT_FLAG"


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


# ---------------------------------------------------------------------------
# PriceHoldReleaseRecipe
# ---------------------------------------------------------------------------

from recipes.PriceHoldReleaseRecipe import execute_price_hold_release


class TestPriceHoldReleaseRecipe:
    def _call(self, *, po_price: float, sap_base_price: float = 100.0,
              tolerance: float = 0.02, hard_block: float = 0.10,
              hold_status: str = "HELD"):
        return execute_price_hold_release(
            order_id="SO-9001", line_item=1,
            po_price=po_price, sap_base_price=sap_base_price,
            tolerance_pct=tolerance, hard_block_pct=hard_block,
            hold_status=hold_status,
        )

    # -- auto-release (within tolerance) -------------------------------------

    def test_variance_at_zero_auto_releases(self):
        r = self._call(po_price=100.0)
        assert r["status"] == "RELEASED"
        assert r["action"] == "AUTO_RELEASE"
        assert r["variance_pct"] == 0.0

    def test_variance_within_tolerance_auto_releases(self):
        r = self._call(po_price=101.5)  # 1.5% variance
        assert r["status"] == "RELEASED"
        assert r["action"] == "AUTO_RELEASE"

    def test_variance_at_tolerance_boundary_auto_releases(self):
        r = self._call(po_price=102.0)  # exactly 2%
        assert r["status"] == "RELEASED"

    # -- escalate (between tolerance and hard_block) -------------------------

    def test_variance_above_tolerance_escalates(self):
        r = self._call(po_price=105.0)  # 5% variance
        assert r["status"] == "REVIEW_REQUIRED"
        assert r["action"] == "ESCALATE"

    def test_variance_at_hard_block_boundary_escalates(self):
        r = self._call(po_price=110.0)  # exactly 10%
        assert r["status"] == "REVIEW_REQUIRED"
        assert r["action"] == "ESCALATE"

    def test_negative_variance_escalates_by_absolute_value(self):
        r = self._call(po_price=95.0)  # -5% — abs value triggers escalate
        assert r["status"] == "REVIEW_REQUIRED"
        assert r["action"] == "ESCALATE"
        assert r["variance_pct"] == -0.05

    # -- hard-block (above hard_block) ---------------------------------------

    def test_variance_above_hard_block_rejects(self):
        r = self._call(po_price=115.0)  # 15% variance
        assert r["status"] == "REJECTED"
        assert r["action"] == "HARD_BLOCK"

    def test_large_negative_variance_rejects(self):
        r = self._call(po_price=50.0)  # -50%
        assert r["status"] == "REJECTED"
        assert r["action"] == "HARD_BLOCK"

    # -- failure cases -------------------------------------------------------

    def test_hold_status_not_held_returns_failed(self):
        r = self._call(po_price=100.0, hold_status="RELEASED")
        assert r["status"] == "FAILED"
        assert "HELD" in r["reason"]

    def test_tolerance_not_less_than_hard_block_raises(self):
        import pytest
        with pytest.raises(AssertionError):
            execute_price_hold_release(
                order_id="SO-9002", line_item=1,
                po_price=100.0, sap_base_price=100.0,
                tolerance_pct=0.10, hard_block_pct=0.05,
                hold_status="HELD",
            )

    # -- payload shape --------------------------------------------------------

    def test_payload_contains_action_and_variance(self):
        r = self._call(po_price=101.0)
        payload = r["payload"]
        assert payload["OrderID"] == "SO-9001"
        assert payload["Action"] == "AUTO_RELEASE"
        assert payload["VariancePct"] == 0.01

    def test_requester_role_passed_through(self):
        r = execute_price_hold_release(
            order_id="SO-9001", line_item=1,
            po_price=100.0, sap_base_price=100.0,
            tolerance_pct=0.02, hard_block_pct=0.10,
            hold_status="HELD",
            requester_role="ORDER_MANAGER",
        )
        assert r["payload"]["RequesterRole"] == "ORDER_MANAGER"

    # -- purity --------------------------------------------------------------

    def test_no_policy_import(self):
        import inspect
        mod = inspect.getmodule(execute_price_hold_release)
        src = inspect.getsource(mod)
        assert "from contracts.policy" not in src, (
            "PriceHoldReleaseRecipe must not import from contracts.policy"
        )


# ---------------------------------------------------------------------------
# EdiMismatchRecipe
# ---------------------------------------------------------------------------

from recipes.EdiMismatchRecipe import detect_edi_mismatch


class TestEdiMismatchRecipe:
    _AUTONOMY = {
        "SKU_MISMATCH": "L3",
        "QTY_MISMATCH": "L2",
        "UOM_MISMATCH": "L2",
        "SHIP_TO_MISMATCH": "L1",
    }

    def _call(self, sub_type: str, *, expected="x", received="y"):
        return detect_edi_mismatch(
            order_id="SO-EDM-1",
            sub_type=sub_type,
            expected_value=expected,
            received_value=received,
            autonomy_levels=self._AUTONOMY,
        )

    # -- SKU_MISMATCH → hard reject -----------------------------------------

    def test_sku_mismatch_hard_rejects(self):
        r = self._call("SKU_MISMATCH", expected="SKU-001", received="SKU-002")
        assert r["status"] == "REJECTED"
        assert r["classification"] == "HARD_REJECT"
        assert r["recommended_action"] == "BLOCK_AND_NOTIFY"

    def test_sku_mismatch_has_notification_template(self):
        r = self._call("SKU_MISMATCH")
        assert r["notification_template"] == "edi_line_mismatch_blocked"

    def test_sku_mismatch_autonomy_level(self):
        r = self._call("SKU_MISMATCH")
        assert r["autonomy_level"] == "L3"

    # -- QTY_MISMATCH / UOM_MISMATCH → review ------------------------------

    def test_qty_mismatch_requires_review(self):
        r = self._call("QTY_MISMATCH", expected=10, received=12)
        assert r["status"] == "REVIEW_REQUIRED"
        assert r["classification"] == "REVIEW"
        assert r["recommended_action"] == "REQUEST_BUYER_CONFIRMATION"

    def test_uom_mismatch_requires_review(self):
        r = self._call("UOM_MISMATCH", expected="EA", received="CS")
        assert r["status"] == "REVIEW_REQUIRED"
        assert r["classification"] == "REVIEW"

    def test_qty_mismatch_autonomy_level(self):
        r = self._call("QTY_MISMATCH")
        assert r["autonomy_level"] == "L2"

    # -- SHIP_TO_MISMATCH → escalate ----------------------------------------

    def test_ship_to_mismatch_escalates(self):
        r = self._call("SHIP_TO_MISMATCH", expected="W-01", received="W-02")
        assert r["status"] == "REVIEW_REQUIRED"
        assert r["classification"] == "ESCALATE"
        assert r["recommended_action"] == "ESCALATE"

    def test_ship_to_mismatch_no_notification(self):
        r = self._call("SHIP_TO_MISMATCH")
        assert r["notification_template"] is None

    def test_ship_to_mismatch_autonomy_l1(self):
        r = self._call("SHIP_TO_MISMATCH")
        assert r["autonomy_level"] == "L1"

    # -- unknown sub_type → FAILED (routes to FAIL_TO_HUMAN upstream) ------

    def test_unknown_sub_type_returns_failed(self):
        r = self._call("WEIRD_SUB_TYPE")
        assert r["status"] == "FAILED"
        assert r["classification"] is None
        assert "WEIRD_SUB_TYPE" in r["reason"]

    def test_price_mismatch_never_reaches_recipe(self):
        # PRICE_MISMATCH is routed to CONTRACTUAL_CORRECTION at classifier
        # time. If it ever reaches this recipe (a routing bug), we fail
        # explicitly rather than silently classifying.
        r = self._call("PRICE_MISMATCH")
        assert r["status"] == "FAILED"

    # -- echoes preserved for audit -----------------------------------------

    def test_expected_received_echoed(self):
        r = self._call("QTY_MISMATCH", expected=10, received=12)
        assert r["expected_value"] == 10
        assert r["received_value"] == 12

    # -- coverage guarantee (CLAUDE.md §3) ----------------------------------

    def test_every_accepted_sub_type_has_a_branch(self):
        from recipes.EdiMismatchRecipe import _CLASSIFICATION_BY_SUB_TYPE
        for sub_type in _CLASSIFICATION_BY_SUB_TYPE:
            r = self._call(sub_type)
            assert r["status"] != "FAILED", (
                f"Accepted sub_type {sub_type!r} must not return FAILED"
            )
            assert r["classification"] is not None

    # -- purity --------------------------------------------------------------

    def test_no_policy_import(self):
        import inspect
        mod = inspect.getmodule(detect_edi_mismatch)
        src = inspect.getsource(mod)
        assert "from contracts.policy" not in src, (
            "EdiMismatchRecipe must not import from contracts.policy"
        )
