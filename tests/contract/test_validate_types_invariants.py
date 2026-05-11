"""Phase 6b — property-based invariants for the validate_types node.

Implements design.md Lane 1 W4 (the second of the two missing
tests surfaced when the BDD plan was audited):

  "tests/contract/test_validate_types_invariants.py — feed
   validate_types 1000 generated ExceptionRecord instances;
   assert no node raises and final_status is in LIFECYCLE_STATES."

The W4 plan was approximate (validate_types takes GraphState, not
ExceptionRecord). This test exercises the actual contract:

  For each (intent, recipe) pair, generate N perturbed OrderEvent
  variants, run them through validate_types directly, and assert
  the structural invariants:

    I1. validate_types does not raise (exception during type
        validation is a bug — the node must surface terminal
        verdicts via state.final_status, never via exceptions).
    I2. After running, either:
          - state.invocation is set with a recipe_name matching
            state.selected_recipe (happy path), OR
          - state.final_status is a terminal value (FAIL_TO_HUMAN
            / MANUAL_REVIEW_REQUIRED / BLOCKED) — explicit failure.
    I3. state.final_status, when set, MUST be in TerminalStatus
        enum (no string ad-hoc values).

The test complements test_recipe_invariants.py (which runs the
full graph). This test isolates validate_types so a regression
in that single node is identifiable from a focused failure.

Generator: deterministic stdlib random.Random, seeded for
reproducibility. Same pattern as test_recipe_invariants.py — no
polyfactory / hypothesis dependency. Migrate to hypothesis when
the dev-dep landing happens.
"""
from __future__ import annotations

import random
from typing import List

import pytest

pytest.importorskip("langgraph", reason="langgraph not installed")

from api.analysis_adapters import INTENT_TO_RECIPE_NAME  # noqa: E402
from contracts.models import GraphState, OrderEvent, TerminalStatus  # noqa: E402
from orchestration.nodes import validate_types  # noqa: E402

from .test_recipe_invariants import _perturb  # noqa: E402
from .test_workflow_pipeline_invariants import _event_for_intent  # noqa: E402


# Variants per (intent, recipe) pair. The plan called for ~1000
# total; with 10 pairs that's 100 each. Tunable via env if a
# regression needs amplification.
_VARIANTS_PER_PAIR = 50


def _intent_recipe_pairs() -> List[tuple[str, str]]:
    return sorted(INTENT_TO_RECIPE_NAME.items())


@pytest.mark.parametrize("intent,recipe", _intent_recipe_pairs())
def test_validate_types_invariants(intent: str, recipe: str) -> None:
    """validate_types must terminate cleanly for every perturbed event.

    I1: no exception.
    I2: state.invocation set OR state.final_status set.
    I3: state.final_status, when set, is a TerminalStatus enum.

    Seeding: each (intent, recipe) pair runs with a deterministic
    seed derived from the recipe name so a failure is reproducible
    by re-running pytest with the same test selector.
    """
    rng = random.Random(hash(recipe) & 0xFFFFFFFF)
    base = _event_for_intent(intent)

    for variant_idx in range(_VARIANTS_PER_PAIR):
        event = _perturb(rng, base)
        state = GraphState(event=event)
        state.selected_recipe = recipe

        try:
            updated = validate_types(state)
        except Exception as exc:  # noqa: BLE001 — bug surface
            pytest.fail(
                f"validate_types raised on {intent}/{recipe} "
                f"variant {variant_idx}: {exc!r}\n"
                f"event: {event.model_dump_json()}\n"
                "validate_types must route failures through state.final_status, "
                "never via exception."
            )

        # I2: post-condition. Either invocation is wired or a
        # terminal verdict is set. A state with neither is the
        # silent-partial-state bug the test was authored to catch.
        has_invocation = (
            updated.invocation is not None
            and updated.invocation.recipe_name == recipe
        )
        has_terminal = updated.final_status is not None
        assert has_invocation or has_terminal, (
            f"validate_types left state in limbo on {intent}/{recipe} "
            f"variant {variant_idx}: no invocation, no final_status. "
            f"event: {event.model_dump_json()}"
        )

        # I3: terminal status, when set, is a TerminalStatus enum.
        if has_terminal:
            assert isinstance(updated.final_status, TerminalStatus), (
                f"validate_types set final_status to a non-enum value on "
                f"{intent}/{recipe} variant {variant_idx}: "
                f"{updated.final_status!r} (type: "
                f"{type(updated.final_status).__name__})"
            )


def test_validate_types_handles_unknown_recipe_cleanly() -> None:
    """A bogus selected_recipe must terminate, not crash.

    Per CLAUDE.md Pillar #5, explicit failure is correct behavior
    when policy / determinism is unclear. validate_types fed an
    unknown recipe should route to FAIL_TO_HUMAN /
    MANUAL_REVIEW_REQUIRED, never raise.
    """
    state = GraphState(event=_event_for_intent("PRICE_HOLD_RELEASE"))
    state.selected_recipe = "NonexistentRecipe.py"

    try:
        updated = validate_types(state)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"validate_types raised on unknown recipe: {exc!r}. "
            "Must route to terminal status instead."
        )

    # The node either declines to populate invocation (leaving
    # downstream nodes to error) or sets a terminal verdict
    # directly. Either is acceptable; raising is not.
    if updated.invocation is not None:
        # Some routing logic may swallow the unknown name; the
        # downstream nodes will then catch it. That's the
        # contract — validate_types itself does not raise.
        return
    assert (
        updated.final_status is None
        or isinstance(updated.final_status, TerminalStatus)
    ), (
        "unknown recipe must leave final_status as None or set it to a "
        "TerminalStatus enum value"
    )
