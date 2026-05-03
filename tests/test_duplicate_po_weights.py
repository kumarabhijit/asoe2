from __future__ import annotations

# ADR-029 V1 §8 — DuplicatePORecipe weight-contract tests.
#
# The gateway is the primary enforcement point — see
# tests/test_tenant_config_gateway.py — these tests cover the recipe's
# defensive validator that runs even when the gateway is bypassed
# (direct calls, test fixtures).
#
# Test class naming: kept distinct from TestDuplicatePORecipe (in
# tests/test_recipes.py) so the ADR-029 surface is reviewable in
# isolation. The behaviours tested here match ADR-029 V1 §8 line by
# line.

import pytest

from recipes.DuplicatePORecipe import (
    _WEIGHTS,
    WeightContractViolation,
    assert_weight_contract,
    detect_duplicate_po,
)


# ---------------------------------------------------------------------------
# DuplicatePORecipe — weight contract & override (ADR-029)
# ---------------------------------------------------------------------------


class TestDuplicatePOWeightOverride:
    """Recipe-side weight-contract validation per ADR-029."""

    def _platform_weights(self) -> dict:
        """Module-default weight map — sums to 1.0."""
        return {
            "po_number": 0.30, "customer_id": 0.15, "line_items": 0.20,
            "amount": 0.10, "timestamp": 0.10, "ship_to": 0.05,
            "channel": 0.05, "delivery_date": 0.05,
        }

    def _perfect_signals(self) -> dict:
        return {k: 1.0 for k in (
            "po_number", "customer_id", "line_items", "amount",
            "timestamp", "ship_to", "channel", "delivery_date",
        )}

    # -- happy path --------------------------------------------------------

    def test_custom_weights_override_changes_score(self):
        """ADR-029 V1 §8 — supply weights summing to 1.0; verify scoring uses them.

        With po_number weighted 1.0 and all others 0.0, only the
        po_number signal contributes to the composite score.
        """
        custom = {
            "po_number": 1.0, "customer_id": 0.0, "line_items": 0.0,
            "amount": 0.0, "timestamp": 0.0, "ship_to": 0.0,
            "channel": 0.0, "delivery_date": 0.0,
        }
        # Signals: po_number=0.5, others=1.0 — composite should be 0.5
        # because only po_number's weight is non-zero.
        signals = {k: 1.0 for k in custom}
        signals["po_number"] = 0.5
        result = detect_duplicate_po(
            "PO-X", "cust-X", signals, weights=custom,
        )
        assert abs(result["composite_score"] - 0.5) < 1e-6
        # Breakdown reflects the override weights.
        assert result["signal_breakdown"]["po_number"] == 0.5
        assert result["signal_breakdown"]["customer_id"] == 0.0

    def test_weights_none_falls_back_to_module_default(self):
        """When weights=None, the recipe uses _WEIGHTS — back-compat."""
        result = detect_duplicate_po(
            "PO-X", "cust-X", self._perfect_signals(), weights=None,
        )
        # All signals=1.0 → composite=1.0 with module defaults.
        assert abs(result["composite_score"] - 1.0) < 1e-6

    def test_weights_kwarg_omitted_falls_back_to_module_default(self):
        """Param defaults to None → identical to explicit weights=None."""
        result = detect_duplicate_po(
            "PO-X", "cust-X", self._perfect_signals(),
        )
        assert abs(result["composite_score"] - 1.0) < 1e-6

    # -- contract violations -----------------------------------------------

    def test_weights_sum_violation_raises(self):
        """ADR-029 V1 §8 — sum=0.95 outside 1e-4 tolerance → violation."""
        weights = self._platform_weights()
        weights["po_number"] = 0.25  # drops sum to 0.95
        with pytest.raises(WeightContractViolation, match="Weight sum"):
            detect_duplicate_po(
                "PO-X", "cust-X", self._perfect_signals(), weights=weights,
            )

    def test_weights_floating_point_tolerance_accepted(self):
        """ADR-029 V1 §8 — sum within 1e-4 of 1.0 must be accepted.

        Calibrated weight outputs land at ±1e-4 to ±1e-5 due to
        floating-point accumulation (per ADR-029 2026-05-10 revision).
        """
        weights = self._platform_weights()
        # Drift +5e-5: sum = 1.00005, well within 1e-4 tolerance.
        weights["po_number"] = 0.30005
        # Should NOT raise.
        result = detect_duplicate_po(
            "PO-X", "cust-X", self._perfect_signals(), weights=weights,
        )
        # Score is approximately 1.0 (+5e-5 drift on po_number only).
        assert abs(result["composite_score"] - 1.0) < 1e-3

    def test_weights_floating_point_tolerance_rejected_above_bound(self):
        """ADR-029 V1 §8 — sum=1.0+5e-4 outside tolerance → violation."""
        weights = self._platform_weights()
        # Drift +5e-4: sum = 1.0005, outside 1e-4 tolerance.
        weights["po_number"] = 0.3005
        with pytest.raises(WeightContractViolation, match="Weight sum"):
            detect_duplicate_po(
                "PO-X", "cust-X", self._perfect_signals(), weights=weights,
            )

    def test_weights_negative_value_raises(self):
        """ADR-029 V1 §8 — negative weight outside [0, 1] → violation."""
        weights = self._platform_weights()
        weights["po_number"] = -0.10  # invalid
        # Sum must still be 1.0 to isolate the range check; offset elsewhere.
        weights["customer_id"] = 0.55
        with pytest.raises(WeightContractViolation, match="outside"):
            detect_duplicate_po(
                "PO-X", "cust-X", self._perfect_signals(), weights=weights,
            )

    def test_weights_value_above_one_raises(self):
        weights = self._platform_weights()
        weights["po_number"] = 1.5  # above 1.0
        weights["customer_id"] = -0.35  # offset to keep sum=1.0
        # Either the negative value or the >1.0 value will be flagged
        # first depending on iteration order — both are violations.
        with pytest.raises(WeightContractViolation):
            detect_duplicate_po(
                "PO-X", "cust-X", self._perfect_signals(), weights=weights,
            )

    def test_weights_unknown_key_raises(self):
        """ADR-029 V1 §8 — extra key → key-set mismatch violation."""
        weights = self._platform_weights()
        weights["unknown_signal"] = 0.0  # extra key
        with pytest.raises(WeightContractViolation, match="key set mismatch"):
            detect_duplicate_po(
                "PO-X", "cust-X", self._perfect_signals(), weights=weights,
            )

    def test_weights_missing_key_raises(self):
        """ADR-029 V1 §8 — missing key → key-set mismatch violation.

        The key check runs before the sum check, so removing a key
        surfaces as a key-set mismatch (not a sum violation).
        """
        weights = self._platform_weights()
        del weights["delivery_date"]
        with pytest.raises(WeightContractViolation, match="key set mismatch"):
            detect_duplicate_po(
                "PO-X", "cust-X", self._perfect_signals(), weights=weights,
            )

    def test_weights_non_numeric_raises(self):
        weights = self._platform_weights()
        weights["po_number"] = "0.30"  # string, not numeric
        with pytest.raises(WeightContractViolation, match="not numeric"):
            detect_duplicate_po(
                "PO-X", "cust-X", self._perfect_signals(), weights=weights,
            )


# ---------------------------------------------------------------------------
# Direct tests on the public validator function. The function is
# exported (no leading underscore) so the gateway can re-use it as the
# single source of truth for the weight contract.
# ---------------------------------------------------------------------------


class TestAssertWeightContractDirect:
    def test_module_default_passes(self):
        # Should not raise on module default.
        assert_weight_contract(dict(_WEIGHTS))

    def test_returns_none_on_success(self):
        # Validator is action-only; returns None implicitly.
        assert assert_weight_contract(dict(_WEIGHTS)) is None

    def test_module_default_keys_match_validator_expectation(self):
        """The validator's expected key set is read from _WEIGHTS at
        function-call time. Ensure _WEIGHTS itself stays in sync with
        the eight signals defined in the spec."""
        assert set(_WEIGHTS.keys()) == {
            "po_number", "customer_id", "line_items", "amount",
            "timestamp", "ship_to", "channel", "delivery_date",
        }
