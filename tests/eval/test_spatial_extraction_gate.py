"""ADR-045 §2.4 / P2.7 — the spatial-extraction eval GATE on the golden set.

`pytest tests/eval -m replay` is the PR gate: the DocumentExtractionGateway runs
in REPLAY (RecordedDocumentExtractionBackend — no live model) over the golden
dataset and is scored against the ground-truth boxes. The gate is CONTAINMENT
(primary) + page-accuracy (zero-tolerance) + coordinate-hallucination + a
confidence-ECE ceiling; thresholds live in `thresholds.yaml`. No CODEOWNERS gate
in this engagement — lowering a threshold is just a reviewed diff.

The `-m live` provider run is nightly only and never gates red-green.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean

import pytest
import yaml

from gateways.document_extraction import (
    DocumentExtractionGateway,
    RecordedDocumentExtractionBackend,
)
from tests.eval.spatial_scorer import (
    confidence_ece,
    containment,
    coordinate_hallucination_rate,
    page_accuracy,
)

_EVAL_ROOT = Path(__file__).resolve().parent
_DATASET = _EVAL_ROOT / "datasets" / "extraction_spatial" / "seed.jsonl"
_THRESHOLDS = yaml.safe_load((_EVAL_ROOT / "thresholds.yaml").read_text())["extraction_spatial"]


def _load_golden():
    rows = []
    for line in _DATASET.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _predict(row) -> list[dict]:
    gw = DocumentExtractionGateway(backend=RecordedDocumentExtractionBackend())
    anchors = gw.extract_anchors(
        attachment_id=f"att-{row['id']}",
        source_sha256=row["sha256"],
        hint={"case": row["case"]},
    )
    return [a.model_dump() for a in anchors]


@pytest.mark.replay
def test_spatial_extraction_meets_eval_gate_on_golden_set():
    rows = _load_golden()
    assert rows, "golden dataset is empty"

    all_preds: list[dict] = []
    all_gold: list[dict] = []
    containments: list[float] = []
    per_row_halluc: list[float] = []

    for row in rows:
        preds = _predict(row)
        gold_by_ref = {g["supports_ref"]: g for g in row["fields"]}
        # Align predictions to gold by supports_ref so page-accuracy compares
        # like-for-like.
        row_preds: list[dict] = []
        row_gold: list[dict] = []
        for p in preds:
            g = gold_by_ref.get(p["supports_ref"])
            assert g is not None, f"unexpected anchor {p['supports_ref']}"
            assert p["bbox"] is not None, (
                f"{p['supports_ref']} degraded to text — golden cases must verify"
            )
            containments.append(containment(p["bbox"], g["bbox"]))
            all_preds.append(p)
            all_gold.append(g)
            row_preds.append(p)
            row_gold.append(g)
        # PARITY-7 — the golden set spans multiple cases that share
        # supports_ref labels (e.g. ``order_entry.customer_po`` appears
        # in every row). ``coordinate_hallucination_rate`` re-pairs by
        # ref via a dict, which silently collapses to last-write-wins
        # when refs repeat across rows. Compute per-row first; the
        # aggregate is the weighted-by-row-size mean of those rates.
        if row_preds:
            per_row_halluc.append(
                coordinate_hallucination_rate(row_preds, row_gold)
            )

    mean_containment = mean(containments)
    page_acc = page_accuracy(all_preds, all_gold)
    halluc = mean(per_row_halluc) if per_row_halluc else 0.0
    # PARITY-7 — ECE is a dataset-level metric (bins of confidence vs
    # correctness across the full population), not a per-row mean.
    # The pre-built all_preds / all_gold arrays are index-aligned, so
    # we can compute correctness without re-pairing by ref — that side-
    # steps the ref-collision bug in confidence_ece's by_ref dict and
    # still gives a statistically valid bin-level calibration check.
    from evals.metrics import expected_calibration_error
    confidences: list[float] = []
    correct: list[bool] = []
    for p, g in zip(all_preds, all_gold):
        conf = p.get("confidence")
        bbox = p.get("bbox")
        if conf is None or bbox is None:
            continue
        confidences.append(float(conf))
        ok = (
            p.get("page") == g.get("page")
            and containment(bbox, g["bbox"]) >= 1.0
        )
        correct.append(bool(ok))
    ece = expected_calibration_error(confidences, correct, n_bins=10)

    assert mean_containment >= _THRESHOLDS["containment_min"], (
        f"containment {mean_containment:.3f} < {_THRESHOLDS['containment_min']}"
    )
    assert page_acc >= _THRESHOLDS["page_accuracy_min"], (
        f"page accuracy {page_acc:.3f} < {_THRESHOLDS['page_accuracy_min']}"
    )
    assert halluc <= _THRESHOLDS["hallucination_rate_max"], (
        f"coordinate-hallucination {halluc:.3f} > {_THRESHOLDS['hallucination_rate_max']}"
    )
    assert ece <= _THRESHOLDS["ece_max"], f"ECE {ece:.3f} > {_THRESHOLDS['ece_max']}"
