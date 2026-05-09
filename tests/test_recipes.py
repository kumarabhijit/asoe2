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
from recipes.EmailOrderEntryRecipe import (
    classify_email_order_entry,
    FLOOR_KEYS,
    ALLOWED_REJECT_REASONS,
)


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
        assert r["error_code"] == "UNKNOWN_SUB_TYPE"
        assert "WEIRD_SUB_TYPE" in r["reason"]

    def test_price_mismatch_never_reaches_recipe(self):
        # PRICE_MISMATCH is routed to CONTRACTUAL_CORRECTION at classifier
        # time. If it ever reaches this recipe (a routing bug), we fail
        # explicitly with a differentiated error code that names the
        # routing invariant (review finding M5).
        r = self._call("PRICE_MISMATCH")
        assert r["status"] == "FAILED"
        assert r["error_code"] == "SUB_TYPE_ROUTING_ERROR"
        assert "routed to CONTRACTUAL_CORRECTION" in r["reason"]

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


# ---------------------------------------------------------------------------
# BackOrderResolutionRecipe
# ---------------------------------------------------------------------------

from recipes.BackOrderResolutionRecipe import resolve_back_order


class TestBackOrderResolutionRecipe:
    def _call(self, *, ordered: float = 100.0, available: float = 50.0, **kw):
        return resolve_back_order(
            order_id="SO-BO-001", sku="SKU-1", ordered_qty=ordered,
            available_qty=available, unit_price=10.0, uom="CS",
            severe_gap_pct=0.50,
            alternate_warehouses=kw.get("alternate_warehouses"),
            substitutes=kw.get("substitutes"),
        )

    def test_no_gap_completes(self):
        r = self._call(ordered=100, available=120)
        assert r["status"] == "COMPLETE"
        assert r["classification"] == "NO_GAP"
        assert r["gap_qty"] == 0.0

    def test_minor_gap_review_required(self):
        r = self._call(ordered=100, available=75)  # 25% gap
        assert r["status"] == "REVIEW_REQUIRED"
        assert r["classification"] == "MINOR_GAP"
        assert r["gap_qty"] == 25.0

    def test_severe_gap_escalates(self):
        r = self._call(ordered=100, available=40)  # 60% gap
        assert r["status"] == "ESCALATE"
        assert r["classification"] == "SEVERE_GAP"

    def test_zero_ordered_fails(self):
        r = self._call(ordered=0, available=0)
        assert r["status"] == "FAILED"

    def test_alt_dc_ranked_first_when_available(self):
        r = self._call(
            ordered=100, available=30,
            alternate_warehouses=[
                {"plant": "DC-W-01", "qty": 200, "eta_days": 2,
                 "freight_delta_per_unit": 0.50},
            ],
        )
        assert r["primary_option"]["type"] == "ALT_DC"

    def test_reschedule_always_present_as_fallback(self):
        r = self._call(ordered=100, available=30)
        types = {o["type"] for o in r["resolution_options"]}
        assert "RESCHEDULE" in types and "SPLIT_SHIPMENT" in types


# ---------------------------------------------------------------------------
# OverMaxTrimRecipe
# ---------------------------------------------------------------------------

from recipes.OverMaxTrimRecipe import trim_over_max


class TestOverMaxTrimRecipe:
    def _call(self, *, total_ordered: float = 150.0, max_qty: float = 100.0, lines=None, costs=None):
        return trim_over_max(
            order_id="SO-OM-001", total_ordered=total_ordered,
            max_qty=max_qty, severe_exceedance_pct=0.50,
            order_lines=lines, unit_cost_per_line=costs,
        )

    def test_no_exceedance_completes(self):
        r = self._call(total_ordered=80, max_qty=100)
        assert r["status"] == "COMPLETE"
        assert r["classification"] == "NO_EXCEEDANCE"

    def test_minor_exceedance_review_required(self):
        r = self._call(total_ordered=130, max_qty=100)  # 30% over
        assert r["status"] == "REVIEW_REQUIRED"
        assert r["classification"] == "MINOR"
        assert r["recommended_action"] == "TRIM_TO_MAX"

    def test_severe_exceedance_escalates(self):
        r = self._call(total_ordered=170, max_qty=100)  # 70% over
        assert r["status"] == "ESCALATE"
        assert r["classification"] == "SEVERE"
        assert r["recommended_action"] == "ESCALATE"

    def test_zero_ordered_fails(self):
        r = self._call(total_ordered=0, max_qty=100)
        assert r["status"] == "FAILED"

    def test_line_over_own_max_gets_trim_action(self):
        lines = [
            {"sku": "A", "description": "A", "qty": 80, "max_line_qty": 60,
             "is_even_layer_item": True},
            {"sku": "B", "description": "B", "qty": 50, "max_line_qty": 50,
             "is_even_layer_item": True},
        ]
        r = self._call(total_ordered=130, max_qty=100, lines=lines)
        by_sku = {row["sku"]: row for row in r["trim_plan"]}
        assert by_sku["A"]["action"] == "TRIM"
        assert by_sku["A"]["trimmed_to"] == 60


# ---------------------------------------------------------------------------
# MOQRoundUpRecipe
# ---------------------------------------------------------------------------

from recipes.MOQRoundUpRecipe import round_up_moq


class TestMOQRoundUpRecipe:
    def _call(self, *, ordered: float = 20.0, moq: float = 48.0):
        return round_up_moq(
            order_id="SO-MOQ-001", sku="SKU-1",
            ordered_qty=ordered, moq_qty=moq,
            unit_cost=5.0, uom="CS",
            severe_shortfall_pct=0.25,
            uplift_review_pct=0.10,
        )

    def test_no_shortfall_completes(self):
        r = self._call(ordered=50, moq=48)
        assert r["status"] == "COMPLETE"
        assert r["classification"] == "NO_SHORTFALL"

    def test_minor_shortfall_round_up(self):
        r = self._call(ordered=40, moq=48)  # 16.7% shortfall
        assert r["status"] == "REVIEW_REQUIRED"
        assert r["classification"] == "MINOR_SHORTFALL"
        assert r["recommended_action"] == "ROUND_UP"
        assert r["round_up_plan"]["action"] == "ROUND_UP"
        assert r["round_up_plan"]["round_up_to"] == 48

    def test_severe_shortfall_escalates(self):
        r = self._call(ordered=25, moq=48)  # ~48% shortfall
        assert r["status"] == "ESCALATE"
        assert r["classification"] == "SEVERE_SHORTFALL"
        assert r["recommended_action"] == "ESCALATE"

    def test_zero_moq_fails(self):
        r = self._call(ordered=10, moq=0)
        assert r["status"] == "FAILED"


# ---------------------------------------------------------------------------
# PalletAlignmentRecipe
# ---------------------------------------------------------------------------

from recipes.PalletAlignmentRecipe import align_pallets


class TestPalletAlignmentRecipe:
    def _call(self, lines):
        return align_pallets(
            order_id="SO-PLT-001", lines=lines,
            min_fill_pct=0.90, broken_layer_fill_pct=1.00,
        )

    def test_fully_aligned_completes(self):
        # 3 full pallets, no loose
        r = self._call([{
            "sku": "A", "description": "A", "layer_qty": 24,
            "pallet_qty": 96, "ordered_qty": 288, "uom": "CS",
        }])
        assert r["status"] == "COMPLETE"
        assert r["classification"] == "NO_VIOLATION"

    def test_broken_layer_review_required(self):
        # 100 cases, 24/layer, 96/pallet → 1 pallet + 4 cases loose
        r = self._call([{
            "sku": "A", "description": "A", "layer_qty": 24,
            "pallet_qty": 96, "ordered_qty": 100, "uom": "CS",
        }])
        assert r["status"] == "REVIEW_REQUIRED"
        assert r["classification"] == "BROKEN_LAYER"
        plan = r["suggested_plan"][0]
        assert plan["suggested"] == 96  # round down to full pallet

    def test_partial_pallet_review_required(self):
        # 120 cases, 24/layer, 96/pallet → 1 pallet + full 1 layer (24).
        # partial fill = 24/96 = 25%  < 90% threshold → PARTIAL_PALLET.
        r = self._call([{
            "sku": "A", "description": "A", "layer_qty": 24,
            "pallet_qty": 96, "ordered_qty": 120, "uom": "CS",
        }])
        assert r["classification"] == "PARTIAL_PALLET"
        plan = r["suggested_plan"][0]
        assert plan["suggested"] == 96

    def test_empty_lines_fails(self):
        r = self._call([])
        assert r["status"] == "FAILED"


# ---------------------------------------------------------------------------
# DeliveryDelayResolutionRecipe
# ---------------------------------------------------------------------------

from recipes.DeliveryDelayResolutionRecipe import resolve_delivery_delay


class TestDeliveryDelayResolutionRecipe:
    def _call(self, planned: str, eta: str, options=None):
        return resolve_delivery_delay(
            order_id="SO-DD-001", planned_date=planned,
            projected_eta=eta, minor_days=2, severe_days=5,
            alternate_options=options,
        )

    def test_on_time_completes(self):
        r = self._call("2026-04-20T00:00:00Z", "2026-04-20T00:00:00Z")
        assert r["status"] == "COMPLETE"
        assert r["classification"] == "ON_TIME"

    def test_minor_delay_review_required(self):
        r = self._call("2026-04-20T00:00:00Z", "2026-04-23T00:00:00Z")  # 3 days
        assert r["status"] == "REVIEW_REQUIRED"
        assert r["classification"] == "MINOR_DELAY"
        assert r["days_late"] == 3

    def test_severe_delay_escalates(self):
        r = self._call("2026-04-20T00:00:00Z", "2026-04-26T00:00:00Z")  # 6 days
        assert r["status"] == "ESCALATE"
        assert r["classification"] == "SEVERE_DELAY"

    def test_invalid_date_fails(self):
        r = self._call("not-a-date", "2026-04-22T00:00:00Z")
        assert r["status"] == "FAILED"

    def test_option_ranking_respects_recommended_pin(self):
        r = self._call(
            "2026-04-20T00:00:00Z", "2026-04-23T00:00:00Z",
            options=[
                {"id": "a", "type": "RESCHEDULE", "extra_cost": 0,
                 "recommended": False},
                {"id": "b", "type": "EXPEDITE", "extra_cost": 620,
                 "recommended": True},
            ],
        )
        assert r["primary_option"]["id"] == "b"

    def test_severe_prefers_reschedule_over_expedite(self):
        r = self._call(
            "2026-04-20T00:00:00Z", "2026-04-27T00:00:00Z",  # 7 days
            options=[
                {"id": "a", "type": "EXPEDITE", "extra_cost": 100,
                 "recommended": False},
                {"id": "b", "type": "RESCHEDULE", "extra_cost": 0,
                 "recommended": False},
            ],
        )
        assert r["primary_option"]["type"] == "RESCHEDULE"

# ---------------------------------------------------------------------------
# Purity: none of the new recipes imports from contracts.policy
# ---------------------------------------------------------------------------

class TestNewRecipePurity:
    def test_no_policy_imports(self):
        import inspect
        for fn in (resolve_back_order, trim_over_max, round_up_moq,
                   align_pallets, resolve_delivery_delay,
                   classify_email_order_entry):
            src = inspect.getsource(inspect.getmodule(fn))
            assert "from contracts.policy" not in src, (
                f"{fn.__module__} must not import from contracts.policy"
            )


# ---------------------------------------------------------------------------
# EmailOrderEntryRecipe (ADR-034)
# ---------------------------------------------------------------------------


def _full_floor() -> dict[str, bool]:
    return {k: True for k in FLOOR_KEYS}


_AUTONOMY = {
    "ONE_CLICK_APPROVE":     "L3",
    "STANDARD_REVIEW":       "L2",
    "LOW_CONFIDENCE_FLAG":   "L1",
    "AUTO_CORRECT":          "L3",
    "REQUEST_CLARIFICATION": "L2",
    "ESCALATE":              "L1",
    "REJECT":                "L1",
}


class TestEmailOrderEntryRecipe:
    """Confidence-band decision tree on a post-extraction envelope."""

    def test_one_click_approve_above_threshold(self):
        out = classify_email_order_entry(
            order_id="EML-1", customer_id="C-1",
            composite_confidence=0.97,
            validation_failures=[],
            non_disableable_floor=_full_floor(),
            autonomy_levels=_AUTONOMY,
        )
        assert out["status"] == "SUCCESS"
        assert out["classification"] == "ONE_CLICK_APPROVE"
        assert out["recommended_action"] == "ONE_CLICK_APPROVE"
        assert out["autonomy_level"] == "L3"
        assert out["floor_breaches"] == []
        assert out["reject_reason_code"] is None

    def test_threshold_boundary_auto_approve_closed_lower_bound(self):
        # 0.95 is the closed lower bound for auto-approve — must classify ONE_CLICK_APPROVE.
        out = classify_email_order_entry(
            order_id="EML-2", customer_id="C-1",
            composite_confidence=0.95,
            validation_failures=[],
            non_disableable_floor=_full_floor(),
            autonomy_levels=_AUTONOMY,
        )
        assert out["classification"] == "ONE_CLICK_APPROVE"

    def test_just_below_auto_approve_threshold_is_standard_review(self):
        out = classify_email_order_entry(
            order_id="EML-3", customer_id="C-1",
            composite_confidence=0.9499,
            validation_failures=[],
            non_disableable_floor=_full_floor(),
            autonomy_levels=_AUTONOMY,
        )
        assert out["classification"] == "STANDARD_REVIEW"
        assert out["recommended_action"] == "STANDARD_REVIEW"
        assert out["autonomy_level"] == "L2"

    def test_review_band_lower_bound_is_closed(self):
        out = classify_email_order_entry(
            order_id="EML-4", customer_id="C-1",
            composite_confidence=0.85,
            validation_failures=[],
            non_disableable_floor=_full_floor(),
            autonomy_levels=_AUTONOMY,
        )
        assert out["classification"] == "STANDARD_REVIEW"

    def test_below_review_band_is_low_confidence(self):
        out = classify_email_order_entry(
            order_id="EML-5", customer_id="C-1",
            composite_confidence=0.84,
            validation_failures=[],
            non_disableable_floor=_full_floor(),
            autonomy_levels=_AUTONOMY,
        )
        assert out["classification"] == "LOW_CONFIDENCE"
        assert out["recommended_action"] == "LOW_CONFIDENCE_FLAG"
        assert out["autonomy_level"] == "L1"

    def test_floor_breach_rejects_even_at_high_confidence(self):
        floor = _full_floor()
        floor["sender_authorized"] = False
        out = classify_email_order_entry(
            order_id="EML-6", customer_id="C-1",
            composite_confidence=0.99,
            validation_failures=[],
            non_disableable_floor=floor,
            autonomy_levels=_AUTONOMY,
        )
        assert out["classification"] == "FATAL_REJECT"
        assert out["recommended_action"] == "REJECT"
        assert out["status"] == "REJECTED"
        assert out["reject_reason_code"] == "sender_unauthorized"
        assert "sender_authorized" in out["floor_breaches"]

    def test_credit_floor_breach_maps_to_credit_block(self):
        floor = _full_floor()
        floor["credit_clear"] = False
        out = classify_email_order_entry(
            order_id="EML-7", customer_id="C-1",
            composite_confidence=0.99,
            validation_failures=[],
            non_disableable_floor=floor,
            autonomy_levels=_AUTONOMY,
        )
        assert out["reject_reason_code"] == "credit_block"

    def test_explicit_reject_reason_overrides_decision_tree(self):
        out = classify_email_order_entry(
            order_id="EML-8", customer_id="C-1",
            composite_confidence=0.99,
            validation_failures=[],
            non_disableable_floor=_full_floor(),
            autonomy_levels=_AUTONOMY,
            reject_reason_code="corrupt_input",
        )
        assert out["classification"] == "FATAL_REJECT"
        assert out["reject_reason_code"] == "corrupt_input"

    def test_unknown_reject_reason_collapses_to_corrupt_input(self):
        out = classify_email_order_entry(
            order_id="EML-9", customer_id="C-1",
            composite_confidence=0.99,
            validation_failures=[],
            non_disableable_floor=_full_floor(),
            autonomy_levels=_AUTONOMY,
            reject_reason_code="not_a_real_code",
        )
        assert out["reject_reason_code"] == "corrupt_input"

    def test_clarification_failure_routes_to_request_clarification(self):
        out = classify_email_order_entry(
            order_id="EML-10", customer_id="C-1",
            composite_confidence=0.99,
            validation_failures=["missing_delivery_date"],
            non_disableable_floor=_full_floor(),
            autonomy_levels=_AUTONOMY,
        )
        assert out["classification"] == "STANDARD_REVIEW"
        assert out["recommended_action"] == "REQUEST_CLARIFICATION"
        assert out["autonomy_level"] == "L2"

    def test_auto_correct_requires_only_correctable_failures(self):
        out = classify_email_order_entry(
            order_id="EML-11", customer_id="C-1",
            composite_confidence=0.99,
            validation_failures=["uom_normalisation_required", "po_number_padding"],
            non_disableable_floor=_full_floor(),
            autonomy_levels=_AUTONOMY,
        )
        assert out["recommended_action"] == "AUTO_CORRECT"
        assert out["status"] == "REVIEW_REQUIRED"

    def test_auto_correct_threshold_is_closed_lower_bound(self):
        out = classify_email_order_entry(
            order_id="EML-12", customer_id="C-1",
            composite_confidence=0.99,
            validation_failures=["po_number_padding"],
            non_disableable_floor=_full_floor(),
            autonomy_levels=_AUTONOMY,
        )
        assert out["recommended_action"] == "AUTO_CORRECT"

    def test_auto_correct_blocked_when_failure_is_not_in_allowlist(self):
        # A pricing-variance failure is internal-team territory, not
        # auto-correctable. Even at 0.99 confidence the recipe must escalate.
        out = classify_email_order_entry(
            order_id="EML-13", customer_id="C-1",
            composite_confidence=0.99,
            validation_failures=["pricing_variance_above_tolerance"],
            non_disableable_floor=_full_floor(),
            autonomy_levels=_AUTONOMY,
        )
        assert out["recommended_action"] == "ESCALATE"
        assert out["autonomy_level"] == "L1"

    def test_validation_failure_in_review_band_with_unknown_failure_escalates(self):
        out = classify_email_order_entry(
            order_id="EML-14", customer_id="C-1",
            composite_confidence=0.90,
            validation_failures=["pricing_variance_above_tolerance"],
            non_disableable_floor=_full_floor(),
            autonomy_levels=_AUTONOMY,
        )
        assert out["recommended_action"] == "ESCALATE"

    def test_missing_floor_keys_default_to_breach(self):
        # Defensive: any missing floor key is treated as breached.
        out = classify_email_order_entry(
            order_id="EML-15", customer_id="C-1",
            composite_confidence=0.99,
            validation_failures=[],
            non_disableable_floor={"sender_authorized": True},  # other keys missing
            autonomy_levels=_AUTONOMY,
        )
        assert out["classification"] == "FATAL_REJECT"
        assert "customer_resolved" in out["floor_breaches"]
        assert "duplicate_po_clear" in out["floor_breaches"]
        assert "credit_clear" in out["floor_breaches"]

    def test_confidence_clamp_does_not_change_echo(self):
        # An out-of-range confidence (>1.0) is clamped for the decision but
        # the original value is preserved on the echoed output for audit.
        out = classify_email_order_entry(
            order_id="EML-16", customer_id="C-1",
            composite_confidence=1.2,
            validation_failures=[],
            non_disableable_floor=_full_floor(),
            autonomy_levels=_AUTONOMY,
        )
        assert out["composite_confidence"] == 1.2
        assert out["classification"] == "ONE_CLICK_APPROVE"

    def test_output_has_all_required_keys(self):
        out = classify_email_order_entry(
            order_id="EML-17", customer_id="C-1",
            composite_confidence=0.5,
            validation_failures=[],
            non_disableable_floor=_full_floor(),
            autonomy_levels=_AUTONOMY,
        )
        for key in (
            "status", "classification", "recommended_action",
            "autonomy_level", "notification_template",
            "composite_confidence", "validation_failures",
            "floor_breaches", "reject_reason_code",
            "order_id", "customer_id",
        ):
            assert key in out, f"missing output key: {key}"

    def test_allowed_reject_reasons_vocabulary(self):
        # Sanity-check that the policy_floor_breach catch-all is in the set.
        assert "policy_floor_breach" in ALLOWED_REJECT_REASONS
