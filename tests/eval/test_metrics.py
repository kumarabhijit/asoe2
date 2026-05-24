"""Unit tests for the pure eval metrics core (evals/metrics.py).

These test implemented code (no model, no project LLM deps) and must pass —
the GREEN counterpart to the xfail harness-skeleton spec. Calibration (ECE) is
the metric the autonomy ladder's safety depends on, so it is tested explicitly,
including the fail-closed paths.
"""

from __future__ import annotations

import pytest

from evals.metrics import (
    confusion_matrix,
    expected_calibration_error,
    field_accuracy,
    hallucination_rate,
    macro_f1,
)


def test_confusion_matrix_counts_actual_by_predicted() -> None:
    cm = confusion_matrix([("A", "A"), ("A", "B"), ("B", "B")])
    assert cm == {"A": {"A": 1, "B": 1}, "B": {"A": 0, "B": 1}}


def test_confusion_matrix_includes_never_true_labels() -> None:
    # "C" only ever appears as a (wrong) prediction — it must still be a row
    # (all-zero, since it's never the truth) AND a column. The single
    # (actual=A, predicted=C) pair lands at [A][C].
    cm = confusion_matrix([("A", "C")])
    assert cm == {"A": {"A": 0, "C": 1}, "C": {"A": 0, "C": 0}}


def test_macro_f1_weights_each_label_equally() -> None:
    f1 = macro_f1([("A", "A"), ("A", "A"), ("B", "B"), ("B", "A")])
    assert f1 == pytest.approx(0.733333, abs=1e-4)


def test_macro_f1_perfect_is_one_empty_is_zero() -> None:
    assert macro_f1([("A", "A"), ("B", "B")]) == pytest.approx(1.0)
    assert macro_f1([]) == 0.0


def test_ece_zero_when_confidence_matches_accuracy() -> None:
    assert expected_calibration_error([0.5, 0.5], [True, False], n_bins=1) == 0.0


def test_ece_flags_overconfidence() -> None:
    # Stated 0.9, actually right half the time -> 0.4 gap.
    assert expected_calibration_error([0.9, 0.9], [True, False]) == pytest.approx(0.4)
    # Stated 0.9, always right -> 0.1 gap.
    assert expected_calibration_error([0.9, 0.9], [True, True]) == pytest.approx(0.1)


@pytest.mark.parametrize(
    "confidences, correct",
    [
        ([0.9], [True, False]),  # length mismatch
        ([], []),                # empty sample
        ([1.5], [True]),         # out of range
    ],
)
def test_ece_fails_closed_on_bad_input(confidences, correct) -> None:
    with pytest.raises(ValueError):
        expected_calibration_error(confidences, correct)


def test_field_accuracy_counts_exact_matches_over_expected() -> None:
    # sku right, qty wrong -> 1/2.
    assert field_accuracy([({"sku": "A", "qty": 5}, {"sku": "A", "qty": 3})]) == 0.5
    assert field_accuracy([({"sku": "A"}, {"sku": "A"})]) == 1.0
    assert field_accuracy([]) == 0.0


def test_hallucination_rate_flags_fabricated_fields() -> None:
    # Predicted a price the ground truth never asserts -> 1 of 2 fields fabricated.
    assert hallucination_rate([({"sku": "A"}, {"sku": "A", "price": 9.99})]) == 0.5
    # A null/empty expected value counts as not-asserted -> still fabrication.
    assert (
        hallucination_rate([({"sku": "A", "price": None}, {"sku": "A", "price": 9.99})])
        == 0.5
    )
    # Faithful prediction -> zero.
    assert hallucination_rate([({"sku": "A", "qty": 5}, {"sku": "A", "qty": 5})]) == 0.0
