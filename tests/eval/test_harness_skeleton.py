"""Eval harness skeleton — the #1 missing test-first artifact (panel 2026-05-24).

Constrained generation guarantees the SHAPE of LLM output, not its CORRECTNESS.
Quality is measured by a golden-dataset eval harness, written FIRST, RED until
the harness exists. Two run modes:

  * `-m replay` (PR gate): score against frozen RecordedGatewayBackend outputs;
    deterministic, no live model. Proves the harness + scorers + thresholds work.
  * `-m live`  (nightly): hit the real model, emit a scorecard, fail nightly only.

Red-green is NEVER gated on a live model.

Implementation target (to be created): a top-level `evals` package exposing
  * load_dataset(task: str) -> list[dict]
  * score_classification(rows, backend) -> dict   # confusion + macro_f1 + ece
  * load_thresholds() -> dict                      # from tests/eval/thresholds.yaml
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    from evals.harness import (  # type: ignore
        load_dataset,
        load_thresholds,
        score_classification,
    )

    _IMPLEMENTED = True
except ImportError:
    _IMPLEMENTED = False

_GATE = "panel 2026-05-24: `evals.harness` not implemented yet (eval-first artifact)"

DATASETS = Path(__file__).parent / "datasets"


@pytest.mark.replay
def test_classification_dataset_dir_exists() -> None:
    # The dataset location is part of the contract even before rows are curated.
    assert (DATASETS / "classification").is_dir()


@pytest.mark.replay
@pytest.mark.xfail(reason=_GATE, strict=True)
def test_harness_loads_dataset_and_thresholds() -> None:
    assert _IMPLEMENTED, _GATE
    thresholds = load_thresholds()
    assert "classification" in thresholds
    rows = load_dataset("classification")
    assert isinstance(rows, list)


@pytest.mark.replay
@pytest.mark.xfail(reason=_GATE, strict=True)
def test_classification_scorer_emits_required_metrics() -> None:
    """The scorer must report confusion, macro-F1, and ECE (calibration)."""
    assert _IMPLEMENTED, _GATE
    from constraints.fallback_backend import DeterministicFallbackBackend

    rows = load_dataset("classification")
    report = score_classification(rows, DeterministicFallbackBackend())
    assert {"confusion", "macro_f1", "ece"} <= set(report)


@pytest.mark.replay
@pytest.mark.xfail(reason=_GATE, strict=True)
def test_invoice_query_is_a_classification_label() -> None:
    """ADR-042 §5b: invoice_query must not silently collapse into OTHER."""
    assert _IMPLEMENTED, _GATE
    thresholds = load_thresholds()
    labels = set(thresholds["classification"].get("labels", []))
    assert "INVOICE_QUERY" in labels
