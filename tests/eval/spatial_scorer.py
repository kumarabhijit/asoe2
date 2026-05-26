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


def iou(pred_bbox: Sequence[float], gt_bbox: Sequence[float]) -> float:
    """Intersection-over-union — DIAGNOSTIC ONLY (ADR-045 §2.4), never a gate.
    Retained so a shifted-but-correct box vs a clipping box can be inspected."""
    ix0 = max(pred_bbox[0], gt_bbox[0])
    iy0 = max(pred_bbox[1], gt_bbox[1])
    ix1 = min(pred_bbox[2], gt_bbox[2])
    iy1 = min(pred_bbox[3], gt_bbox[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    union = _area(pred_bbox) + _area(gt_bbox) - inter
    return inter / union if union > 0 else 0.0


def coordinate_hallucination_rate(
    preds: List[Dict[str, Any]], gold: List[Dict[str, Any]],
) -> float:
    """Fraction of predicted anchors whose geometry is NOT grounded in the gold
    box (containment below 1.0, or a missing/extra prediction). The runtime
    verifier (ADR-045 §2.2) keeps these out of production; this measures how
    often selection mis-grounded so the trend is visible (ADR-045 §2.4)."""
    if not preds:
        return 0.0
    by_ref = {g.get("supports_ref"): g for g in gold}
    bad = 0
    for p in preds:
        g = by_ref.get(p.get("supports_ref"))
        if g is None or p.get("bbox") is None:
            bad += 1
            continue
        if containment(p["bbox"], g["bbox"]) < 1.0:
            bad += 1
    return bad / len(preds)


def confidence_ece(
    preds: List[Dict[str, Any]], gold: List[Dict[str, Any]], *, n_bins: int = 10,
) -> float:
    """Expected Calibration Error of per-anchor confidence (ADR-045 §2.4):
    "correct" == the predicted box contains the gold box on the right page.
    Delegates to the shared, fail-closed ECE core (`evals.metrics`)."""
    from evals.metrics import expected_calibration_error

    by_ref = {g.get("supports_ref"): g for g in gold}
    confidences: List[float] = []
    correct: List[bool] = []
    for p in preds:
        conf = p.get("confidence")
        bbox = p.get("bbox")
        if conf is None or bbox is None:
            continue
        g = by_ref.get(p.get("supports_ref"))
        ok = (
            g is not None
            and p.get("page") == g.get("page")
            and containment(bbox, g["bbox"]) >= 1.0
        )
        confidences.append(float(conf))
        correct.append(bool(ok))
    return expected_calibration_error(confidences, correct, n_bins=n_bins)
