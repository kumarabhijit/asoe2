"""Pure-stdlib eval metrics — the measurement core the 2026-05-24 panel found
missing (constrained generation guarantees shape, not correctness).

Deterministic, dependency-free, so they are unit-testable without a live model.
Used by the classification / extraction / shadow scorers in ``evals.harness``.

Three metrics:
  * ``confusion_matrix`` — actual -> predicted counts.
  * ``macro_f1`` — unweighted mean per-label F1 (treats every class equally,
    so a rare class like INVOICE_QUERY can't be hidden by a common one).
  * ``expected_calibration_error`` — the gap between stated confidence and
    observed accuracy. This is the metric that decides whether the autonomy
    ladder is sound or theatre: routing money on an uncalibrated scalar is the
    central risk the panel flagged.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence


def confusion_matrix(
    pairs: Iterable[tuple[str, str]],
) -> dict[str, dict[str, int]]:
    """Return ``matrix[actual][predicted] = count`` over (actual, predicted).

    Rows and columns span every label seen on either side, so a label that is
    only ever mispredicted (never the truth) still appears.
    """
    pairs = list(pairs)
    labels = sorted({a for a, _ in pairs} | {p for _, p in pairs})
    matrix = {a: {p: 0 for p in labels} for a in labels}
    for actual, predicted in pairs:
        matrix[actual][predicted] += 1
    return matrix


def _per_label_f1(pairs: Sequence[tuple[str, str]], label: str) -> float:
    tp = sum(1 for a, p in pairs if a == label and p == label)
    fp = sum(1 for a, p in pairs if a != label and p == label)
    fn = sum(1 for a, p in pairs if a == label and p != label)
    if tp == 0:
        return 0.0
    precision = tp / (tp + fp)
    recall = tp / (tp + fn)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def macro_f1(pairs: Iterable[tuple[str, str]]) -> float:
    """Unweighted mean of per-label F1 over all labels present in truth."""
    pairs = list(pairs)
    truth_labels = sorted({a for a, _ in pairs})
    if not truth_labels:
        return 0.0
    return sum(_per_label_f1(pairs, lbl) for lbl in truth_labels) / len(truth_labels)


def field_accuracy(
    records: Iterable[tuple[dict[str, object], dict[str, object]]],
) -> float:
    """Per-field exact-match accuracy over (expected, predicted) record pairs.

    Denominator is the count of expected fields (so omitting a field counts
    against you); numerator is fields whose predicted value exactly matches.
    """
    records = list(records)
    total = sum(len(exp) for exp, _ in records)
    if total == 0:
        return 0.0
    hits = sum(
        1
        for exp, pred in records
        for key, val in exp.items()
        if pred.get(key) == val
    )
    return hits / total


def _is_present(value: object) -> bool:
    return value not in (None, "")


def hallucination_rate(
    records: Iterable[tuple[dict[str, object], dict[str, object]]],
) -> float:
    """Rate of *fabricated* predicted fields — a non-empty predicted value for
    a field the ground truth does not assert (absent, or null/empty).

    This is the metric that maps to dollars: a hallucinated price / qty / SKU
    is a fabricated order line. Denominator is all non-empty predicted fields.

    NOTE: this is the dataset-level approximation (no source spans yet). The
    rigorous span-grounded definition ("asserted a field with no supporting
    span in the source") lands with the extraction gateway (ADR-042 Phase 3).
    """
    records = list(records)
    predicted_present = sum(
        1 for _, pred in records for v in pred.values() if _is_present(v)
    )
    if predicted_present == 0:
        return 0.0
    fabricated = sum(
        1
        for exp, pred in records
        for key, val in pred.items()
        if _is_present(val) and not _is_present(exp.get(key))
    )
    return fabricated / predicted_present


def expected_calibration_error(
    confidences: Sequence[float],
    correct: Sequence[bool],
    *,
    n_bins: int = 10,
) -> float:
    """Expected Calibration Error (ECE) over equal-width confidence bins.

    ECE = sum over bins of (bin_size / N) * |avg_confidence - accuracy|.
    0.0 == perfectly calibrated. Raises on mismatched / empty input or
    out-of-range confidences (fail-closed: a calibration number that silently
    swallows bad input is worse than none).
    """
    if len(confidences) != len(correct):
        raise ValueError("confidences and correct must be the same length")
    n = len(confidences)
    if n == 0:
        raise ValueError("cannot compute ECE over an empty sample")
    if n_bins < 1:
        raise ValueError("n_bins must be >= 1")
    if any(not (0.0 <= c <= 1.0) for c in confidences):
        raise ValueError("confidences must lie in [0, 1]")

    bins: list[list[tuple[float, bool]]] = [[] for _ in range(n_bins)]
    for conf, ok in zip(confidences, correct):
        # conf == 1.0 lands in the last bin.
        idx = min(int(conf * n_bins), n_bins - 1)
        bins[idx].append((conf, bool(ok)))

    ece = 0.0
    for bucket in bins:
        if not bucket:
            continue
        size = len(bucket)
        avg_conf = sum(c for c, _ in bucket) / size
        accuracy = sum(1 for _, ok in bucket if ok) / size
        ece += (size / n) * abs(avg_conf - accuracy)
    return ece
