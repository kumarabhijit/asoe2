"""CP-B RED gate (ADR-045 §2.4) — spatial extraction eval metrics.

Test-first (`xfail(strict=True)`; removed at CP-F; threshold changes gated by the
`thresholds.yaml` CODEOWNERS rule). The panel's metric decision: gate on
CONTAINMENT (does the predicted box contain the ground-truth tokens) + page
accuracy with ZERO tolerance for a wrong page — not IoU (which rewards a
high-overlap box that clips a digit and penalises a shifted-but-correct one).
IoU stays a diagnostic only.
"""

from __future__ import annotations

import pytest


def test_containment_rewards_a_box_that_contains_the_ground_truth():
    from tests.eval.spatial_scorer import containment

    gt = [0.10, 0.10, 0.20, 0.14]
    enclosing = [0.08, 0.08, 0.25, 0.16]   # contains gt → full credit
    clipping = [0.10, 0.10, 0.18, 0.14]    # high IoU but clips the right edge
    assert containment(enclosing, gt) == pytest.approx(1.0)
    assert containment(clipping, gt) < 1.0


def test_wrong_page_is_zero_tolerance():
    from tests.eval.spatial_scorer import page_accuracy

    preds = [{"page": 1}, {"page": 2}]
    gold = [{"page": 1}, {"page": 1}]  # one wrong page
    assert page_accuracy(preds, gold) < 1.0  # any wrong page fails the gate
