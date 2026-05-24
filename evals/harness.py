"""Eval harness — dataset loading + classification scoring.

The eval-first artifact (panel 2026-05-24). Two run modes drive it (see
``tests/eval/``): ``replay`` (PR gate, deterministic backend / recorded
fixtures) and ``live`` (nightly, real model). This module is mode-agnostic:
it scores whatever backend it is handed against a frozen golden dataset.

Fixtures live under ``tests/eval/`` (golden datasets are test data):
  * ``tests/eval/thresholds.yaml`` — per-task label vocabulary + gates.
  * ``tests/eval/datasets/<task>/*.jsonl`` — one JSON object per line,
    ``{"input": {<OrderEvent kwargs>}, "expected": "<INTENT>"}``.

Scoring delegates the maths to ``evals.metrics`` (pure, separately tested).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

import yaml

from evals.metrics import (
    confusion_matrix,
    expected_calibration_error,
    macro_f1,
)

# Golden fixtures are test data — they live under tests/eval/.
_EVAL_ROOT = Path(__file__).resolve().parent.parent / "tests" / "eval"


class _IntentBackend(Protocol):
    """Minimal surface the scorer needs — matches DeterministicFallbackBackend
    and the live/recorded backends (constraints.specs.IntentDecision)."""

    def classify_intent(self, state: Any) -> Any: ...


def load_thresholds(root: Path | None = None) -> dict[str, Any]:
    """Load the per-task eval gates (labels + thresholds)."""
    base = root or _EVAL_ROOT
    return yaml.safe_load((base / "thresholds.yaml").read_text()) or {}


def load_dataset(task: str, root: Path | None = None) -> list[dict[str, Any]]:
    """Load every JSONL row for ``task`` from ``datasets/<task>/``."""
    base = (root or _EVAL_ROOT) / "datasets" / task
    rows: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                rows.append(json.loads(line))
    return rows


def score_classification(
    rows: list[dict[str, Any]],
    backend: _IntentBackend,
) -> dict[str, Any]:
    """Score a classification backend against golden rows.

    Returns confusion matrix, macro-F1, ECE (confidence calibration vs
    correctness), accuracy, and n. Empty input scores zero rather than
    raising — an empty golden set is a curation gap, surfaced by ``n == 0``,
    not a crash.
    """
    # Local import keeps the pure-metrics path (evals.metrics) free of the
    # contracts/pydantic dependency.
    from contracts.models import GraphState, OrderEvent

    pairs: list[tuple[str, str]] = []
    confidences: list[float] = []
    correct: list[bool] = []

    for row in rows:
        expected = row["expected"]
        state = GraphState(event=OrderEvent(**row["input"]))
        decision = backend.classify_intent(state)
        predicted = decision.intent
        pairs.append((expected, predicted))
        confidences.append(float(decision.confidence))
        correct.append(predicted == expected)

    n = len(pairs)
    accuracy = (sum(correct) / n) if n else 0.0
    ece = expected_calibration_error(confidences, correct) if n else 0.0
    return {
        "n": n,
        "accuracy": accuracy,
        "macro_f1": macro_f1(pairs),
        "ece": ece,
        "confusion": confusion_matrix(pairs),
    }
