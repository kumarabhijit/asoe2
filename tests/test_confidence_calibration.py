"""DoR gate #4 (Phase 8) — confidence calibration + no hardcoded autonomy bands.

Two halves of the gate:

  1. **Calibration has teeth.** The eval harness's ECE scorer must hold the
     `classification.ece_max` budget from `tests/eval/thresholds.yaml`: a
     well-calibrated sample passes, an over-confident sample fails. (The live
     ECE measurement runs nightly over the replay dataset; this deterministic
     gate locks the scorer + threshold wiring so the budget can't silently go
     missing.)

  2. **The autonomy gate is policy-driven, not literal-driven.** The
     email-order intake band decision must read the named
     `contracts/policy.py` confidence constants — never inline `0.95`/`0.99`
     in the orchestration logic (the partial-truth / drift risk the DoR flags).
"""

from __future__ import annotations

import ast
from pathlib import Path

from contracts.policy import (
    EMAIL_ORDER_AUTO_APPROVE_CONFIDENCE,
    EMAIL_ORDER_AUTO_CORRECT_CONFIDENCE,
)
from evals.harness import load_thresholds
from evals.metrics import expected_calibration_error

_NODES = Path(__file__).resolve().parent.parent / "orchestration" / "nodes.py"


def _ece_budget() -> float:
    return float(load_thresholds()["classification"]["ece_max"])


def test_well_calibrated_sample_is_within_budget() -> None:
    # Exactly calibrated: a 0.5-confidence bin with 50% accuracy → ECE 0,
    # comfortably within budget.
    assert expected_calibration_error([0.5, 0.5], [True, False], n_bins=1) <= _ece_budget()


def test_overconfident_sample_breaches_budget() -> None:
    # 0.99 confidence but only 50% correct → ECE ~0.49, must exceed the budget
    # (proves the gate is not vacuous).
    ece = expected_calibration_error([0.99, 0.99], [True, False], n_bins=1)
    assert ece > _ece_budget()


def test_autonomy_bands_are_named_policy_constants() -> None:
    # The constants exist and carry the documented spec values (ADR-034 §4).
    assert EMAIL_ORDER_AUTO_APPROVE_CONFIDENCE == 0.95
    assert EMAIL_ORDER_AUTO_CORRECT_CONFIDENCE == 0.99


def test_intake_gate_references_policy_constants_not_inline_literals() -> None:
    # Source-level guard: orchestration/nodes.py must reach the confidence bands
    # by the named policy constants, never by inlining 0.95 / 0.99 as the band
    # threshold in the gate logic (DoR #4 — no hardcoded autonomy thresholds).
    tree = ast.parse(_NODES.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "EMAIL_ORDER_AUTO_APPROVE_CONFIDENCE" in imported
    assert "EMAIL_ORDER_AUTO_CORRECT_CONFIDENCE" in imported

    # No bare 0.95 / 0.99 float literal anywhere in nodes.py — the bands must be
    # the imported names, so a reviewer can't reintroduce a drifting literal.
    literals = {
        round(float(node.value), 4)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    }
    assert 0.95 not in literals, "inline 0.95 literal in nodes.py — use the policy constant"
    assert 0.99 not in literals, "inline 0.99 literal in nodes.py — use the policy constant"
