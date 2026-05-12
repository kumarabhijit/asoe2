"""Phase 8 / Item 20 — Hypothesis-driven property invariants.

Companion to test_recipe_invariants.py, which uses a deterministic
stdlib random.Random for variant generation. Hypothesis adds two
properties the stdlib fuzzer can't:

  1. SHRINKING. When a property fails, Hypothesis searches for the
     minimal failing input — far easier to debug than a hand-rolled
     reproducer.
  2. ARBITRARY STRATEGIES. The shape of an OrderEvent variant is
     declared with `strategies.builds()` / `strategies.from_type()`
     and exhaustively explored, rather than perturbed around a seed.

This file mirrors the structural invariants in
test_recipe_invariants.py (I1-I3) but for a SINGLE recipe path
(PriceHoldReleaseRecipe), exercised via the validate_types node
directly. Keeping the surface narrow:
  - Validates the migration footprint without re-fuzzing every
    (intent, recipe) pair (the stdlib test still covers all 10).
  - Demonstrates the Hypothesis pattern for future ports.

The stdlib test in test_recipe_invariants.py REMAINS until each
pair is ported. Removing it is a follow-up PR once Hypothesis has
proven equivalent coverage and reproducibility.

Reference: docs/test-strategy/design.md Lane 1 Week 4; CLAUDE.md
guardrail "Errors must be explicit and structured".
"""
from __future__ import annotations

import pytest

pytest.importorskip("hypothesis", reason="hypothesis not installed")
pytest.importorskip("langgraph", reason="langgraph not installed")

from hypothesis import HealthCheck, given, settings, strategies as st

from contracts.models import GraphState, OrderEvent, TerminalStatus
from orchestration.nodes import validate_types

from .test_workflow_pipeline_invariants import _event_for_intent


# Hypothesis strategy: a structurally-valid OrderEvent variant
# rooted at the PriceHoldReleaseRecipe seed. Strategies bind to
# the OrderEvent fields the recipe actually reads; other fields
# inherit the seed value via model_copy so we don't drift outside
# the recipe's expected input space.
_BASE = _event_for_intent("PRICE_HOLD_RELEASE")


@st.composite
def order_event_for_price_hold(draw: st.DrawFn) -> OrderEvent:
    """Generate an OrderEvent shaped like a PRICE_HOLD_RELEASE input.

    Strategy bounds:
      - po_price: positive float in [0.01, 10_000.00]
      - sap_base_price: positive float in [0.01, 10_000.00]
      - line_item: int in [1, 9]
      - retailer_id: a sampled identifier
    """
    return _BASE.model_copy(update={
        "po_price": draw(
            st.floats(
                min_value=0.01,
                max_value=10_000.00,
                allow_nan=False,
                allow_infinity=False,
            ),
        ),
        "sap_base_price": draw(
            st.floats(
                min_value=0.01,
                max_value=10_000.00,
                allow_nan=False,
                allow_infinity=False,
            ),
        ),
        "line_item": draw(st.integers(min_value=1, max_value=9)),
        "retailer_id": draw(
            st.sampled_from(["R-01", "R-02", "R-10", "R-99"]),
        ),
        "metadata": dict(_BASE.metadata),
    })


@given(event=order_event_for_price_hold())
@settings(
    max_examples=50,
    # The fixture mutates module-level state in some imports
    # (langgraph compiles the graph lazily). Suppress the
    # function-scope health check; Hypothesis only cares the
    # function is pure with respect to inputs, which it is.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_validate_types_invariants_for_price_hold(event: OrderEvent) -> None:
    """validate_types must terminate cleanly on every generated event.

    I1: no exception.
    I2: post-condition — state.invocation set OR final_status set.
    I3: final_status, when set, is a TerminalStatus enum.

    Failure surfaces the shrunk OrderEvent — the minimal po_price /
    sap_base_price combination that triggers the bug.
    """
    state = GraphState(event=event)
    state.selected_recipe = "PriceHoldReleaseRecipe.py"

    updated = validate_types(state)

    has_invocation = (
        updated.invocation is not None
        and updated.invocation.recipe_name == "PriceHoldReleaseRecipe.py"
    )
    has_terminal = updated.final_status is not None
    assert has_invocation or has_terminal, (
        "validate_types left state in limbo (no invocation, no final_status): "
        f"po_price={event.po_price}, sap_base_price={event.sap_base_price}"
    )
    if has_terminal:
        assert isinstance(updated.final_status, TerminalStatus), (
            f"validate_types set final_status to a non-enum value: "
            f"{updated.final_status!r}"
        )
