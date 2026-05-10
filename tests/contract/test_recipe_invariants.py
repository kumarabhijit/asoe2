"""Spec-as-oracle: property-based recipe invariants.

For each (intent, recipe) pair declared in `INTENT_TO_RECIPE_NAME`, drive
the full LangGraph pipeline N times with randomly-perturbed event
parameters and assert the structural invariants:

  I1. Pipeline terminates (final_status not None).
  I2. final_status is a valid `TerminalStatus` enum value.
  I3. final_status maps to a known lifecycle.
  I4. ExecutionLog is populated with intent_selected and trace_id.
  I5. Constrained outputs (intent / shadow / recipe) are recorded when
      their nodes ran (no None-vs-missing ambiguity).

The generator is seeded so failures are reproducible — a regression
prints the seed and the failing event payload.

Hypothesis / polyfactory are NOT a hard dependency; the generator is a
deterministic stdlib `random.Random` bounded to the OrderEvent schema.
The day asoe2 takes a hypothesis dev dep, replace `_perturb` with a
hypothesis strategy and keep the assertions.

Reference: docs/test-strategy/design.md Lane 1 Week 4.
"""

from __future__ import annotations

import random
from typing import Iterable, List, Tuple

import pytest

pytest.importorskip("langgraph", reason="langgraph not installed")

from api.analysis_adapters import INTENT_TO_RECIPE_NAME  # noqa: E402
from contracts.models import (  # noqa: E402
    GraphState,
    LIFECYCLE_STATES,
    OrderEvent,
    STATUS_TO_LIFECYCLE,
    TerminalStatus,
)
from orchestration.graph import run_graph  # noqa: E402

from .test_workflow_pipeline_invariants import _event_for_intent  # noqa: E402


# Number of randomized variants per (intent, recipe) pair. Kept small to
# stay within the existing pytest budget; raise locally to amplify
# fuzzing power.
_VARIANTS_PER_PAIR = 5


def _perturb(rng: random.Random, base: OrderEvent) -> OrderEvent:
    """Return a structurally-valid variant of `base`.

    Perturbs:
      - po_price ± 50% (clamped > 0)
      - sap_base_price ± 30% (clamped > 0)
      - line_item ∈ [1, 5]
      - retailer_id swap
      - copies metadata to avoid mutating shared dicts

    Does NOT change event_type — that would re-route the intent and
    invalidate the pair under test.
    """
    return base.model_copy(update={
        "po_price": max(0.01, base.po_price * (0.5 + rng.random())),
        "sap_base_price": max(
            0.01, base.sap_base_price * (0.7 + rng.random() * 0.6),
        ),
        "line_item": rng.randint(1, 5),
        "retailer_id": rng.choice(["R-01", "R-02", "R-10", "R-99"]),
        "metadata": dict(base.metadata),
    })


def _ids(pair: Tuple[str, str], variant: int) -> str:
    return f"{pair[0]}->{pair[1]}#{variant}"


_PAIRS: List[Tuple[str, str]] = sorted(INTENT_TO_RECIPE_NAME.items())
_PARAMS: List[Tuple[str, str, int]] = [
    (intent, recipe, variant)
    for intent, recipe in _PAIRS
    for variant in range(_VARIANTS_PER_PAIR)
]


@pytest.mark.parametrize(
    "intent,expected_recipe,variant",
    _PARAMS,
    ids=[_ids((i, r), v) for i, r, v in _PARAMS],
)
def test_recipe_invariants_under_random_perturbation(
    intent: str, expected_recipe: str, variant: int,
) -> None:
    """For every randomized variant, the pipeline must satisfy I1–I5."""
    # Per-(intent, variant) seed → reproducible failures.
    seed = hash((intent, variant)) & 0xFFFFFFFF
    rng = random.Random(seed)
    base = _event_for_intent(intent)
    event = _perturb(rng, base)

    state = GraphState(event=event)
    try:
        result = run_graph(state)
    except Exception as exc:
        pytest.fail(
            f"Pipeline raised on intent={intent!r} seed={seed} "
            f"event={event.model_dump()!r}: {exc!r}"
        )

    # I1
    assert result.final_status is not None, (
        f"final_status=None — pipeline returned without terminating. "
        f"seed={seed} event={event.model_dump()}"
    )
    # I2
    valid = {s.value for s in TerminalStatus}
    assert result.final_status.value in valid, (
        f"final_status={result.final_status!r} not in TerminalStatus. "
        f"seed={seed}"
    )
    # I3
    lifecycle = STATUS_TO_LIFECYCLE.get(result.final_status.value)
    assert lifecycle in LIFECYCLE_STATES, (
        f"final_status={result.final_status.value!r} routes to "
        f"lifecycle={lifecycle!r} not in LIFECYCLE_STATES. seed={seed}"
    )
    # I4: when execute_recipe ran, the audit fields are populated.
    # Some events halt before execute_recipe (FAIL_TO_HUMAN at
    # validate_circuit_breaker / shadow / resolve_dependencies); for
    # those, execution_log is legitimately None.
    log = result.execution_log
    if log is not None:
        assert log.intent_selected, "execution_log.intent_selected empty"
        assert log.trace_id, "execution_log.trace_id empty"
        # I5: when execute_recipe ran, all three constrained outputs
        # must be recorded — never an empty string or unknown sentinel.
        co = log.constrained_outputs or {}
        for key in ("intent", "shadow", "recipe"):
            assert key in co and co[key], (
                f"execute_recipe ran but constrained_outputs.{key} "
                f"missing. seed={seed} co={co}"
            )
    else:
        # Even when execute_recipe did not run, the classifier's intent
        # MUST have been recorded on the state — this is the minimum
        # audit trail.
        assert result.intent is not None, (
            f"final_status={result.final_status.value!r} but no intent "
            f"set on state. seed={seed} event={event.model_dump()}"
        )
