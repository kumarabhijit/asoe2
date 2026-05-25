"""Spatial extraction eval scorers (ADR-045 §2.4).

CONTAINMENT is the primary gate — does the predicted box contain the
ground-truth region — not IoU, which rewards a high-overlap box that clips a
digit and penalises a shifted-but-correct box. PAGE ACCURACY is zero-tolerance:
a wrong-page highlight is a trust-killer, so the gate requires 1.0.

bbox format: ``[x0, y0, x1, y1]`` normalised to the page (0..1).
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence


def _area(b: Sequence[float]) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def containment(pred_bbox: Sequence[float], gt_bbox: Sequence[float]) -> float:
    """Fraction of the ground-truth box covered by the predicted box
    (intersection area / ground-truth area). 1.0 when the prediction fully
    encloses the ground truth; < 1.0 when it clips any part of it."""
    gt = _area(gt_bbox)
    if gt <= 0:
        return 0.0
    ix0 = max(pred_bbox[0], gt_bbox[0])
    iy0 = max(pred_bbox[1], gt_bbox[1])
    ix1 = min(pred_bbox[2], gt_bbox[2])
    iy1 = min(pred_bbox[3], gt_bbox[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    return inter / gt


def page_accuracy(preds: List[Dict[str, Any]], gold: List[Dict[str, Any]]) -> float:
    """Fraction of anchors whose predicted page equals the gold page. The gate
    requires 1.0 — any wrong page fails it (zero tolerance)."""
    if not gold:
        return 1.0
    correct = sum(1 for p, g in zip(preds, gold) if p.get("page") == g.get("page"))
    return correct / len(gold)
