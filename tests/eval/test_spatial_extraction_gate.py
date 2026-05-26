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

    for row in rows:
        preds = _predict(row)
        gold_by_ref = {g["supports_ref"]: g for g in row["fields"]}
        # Align predictions to gold by supports_ref so page-accuracy compares
        # like-for-like.
        for p in preds:
            g = gold_by_ref.get(p["supports_ref"])
            assert g is not None, f"unexpected anchor {p['supports_ref']}"
            assert p["bbox"] is not None, (
                f"{p['supports_ref']} degraded to text — golden cases must verify"
            )
            containments.append(containment(p["bbox"], g["bbox"]))
            all_preds.append(p)
            all_gold.append(g)

    mean_containment = mean(containments)
    page_acc = page_accuracy(all_preds, all_gold)
    halluc = coordinate_hallucination_rate(all_preds, all_gold)
    ece = confidence_ece(all_preds, all_gold)

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
