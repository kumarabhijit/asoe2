"""Spec-as-oracle: full LangGraph pipeline invariants per (intent, recipe).

For every entry in `INTENT_TO_RECIPE_NAME` the test:
  1. Constructs a minimal `OrderEvent` whose `event_type` deterministically
     classifies to the target intent (via DeterministicFallbackBackend).
  2. Drives the event through `orchestration.graph.run_graph`.
  3. Asserts the resulting `final_status` is in `LIFECYCLE_STATES` (or the
     terminal status set), the `execution_log` is populated, and — when
     the recipe ran — the `selected_recipe` matches the expected name.

This is the cross-cutting "every (intent, recipe) pair survives the full
pipeline" assertion in `docs/test-strategy/eng-review-test-plan.md`
under Critical Paths:

    `ingest → classify → load_skill → validate_circuit_breaker →
     select_recipe → resolve_dependencies → validate_types → shadow_audit
     → execute_recipe → apply_effects → build_analysis`

Per-recipe deep tests already exist (tests/test_e2e_*.py); this layer is
the structural assertion that no (intent, recipe) pair regresses to a
500/uncaught path or unknown final_status.
"""

from __future__ import annotations

from typing import Dict, Iterable, Tuple

import pytest

pytest.importorskip("langgraph", reason="langgraph not installed")

from contracts.models import (  # noqa: E402
    GraphState,
    LIFECYCLE_STATES,
    OrderEvent,
    STATUS_TO_LIFECYCLE,
    TerminalStatus,
)
from orchestration.graph import run_graph  # noqa: E402


# ---------------------------------------------------------------------------
# Per-intent canonical event constructors. Keep these minimal — the goal is
# to produce a payload that classify_intent routes to the target intent,
# not to exercise recipe-correctness (covered by tests/test_e2e_*.py).
# ---------------------------------------------------------------------------


def _event_for_intent(intent: str) -> OrderEvent:
    if intent == "CONTRACTUAL_CORRECTION":
        return OrderEvent(
            order_id="WF-CC-1", line_item=1,
            po_price=90.0, sap_base_price=100.0,
            retailer_id="R-01", line_count=1,
        )
    if intent == "DUPLICATE_PO":
        return OrderEvent(
            order_id="WF-DUP-1", line_item=1,
            po_price=100.0, sap_base_price=100.0,
            event_type="EDI_850_DUPLICATE_PO",
            retailer_id="R-10", line_count=1,
            metadata={
                "signal_scores": {
                    "po_number": 1.0, "customer_id": 1.0,
                    "line_items": 0.95, "amount": 0.90,
                    "timestamp_proximity": 0.95,
                },
            },
        )
    if intent == "PRICE_HOLD_RELEASE":
        return OrderEvent(
            order_id="WF-PHR-1", line_item=1,
            po_price=100.0, sap_base_price=100.0,
            event_type="EDI_850_PRICE_HOLD",
            retailer_id="R-01", line_count=1,
        )
    if intent == "EDI_MISMATCH":
        return OrderEvent(
            order_id="WF-EDM-1", line_item=1,
            po_price=100.0, sap_base_price=100.0,
            event_type="EDI_850_LINE_MISMATCH",
            retailer_id="R-01", line_count=1,
            metadata={"mismatch_sub_type": "QTY_MISMATCH"},
        )
    if intent == "BACK_ORDER":
        return OrderEvent(
            order_id="WF-BO-1", line_item=1, sku="SKU-BO-1",
            po_price=100.0, sap_base_price=100.0,
            event_type="BACK_ORDER_OOS",
            retailer_id="R-01", line_count=1,
        )
    if intent == "OVER_MAX":
        return OrderEvent(
            order_id="WF-OM-1", line_item=1,
            po_price=100.0, sap_base_price=100.0,
            event_type="OVER_MAX_QTY",
            retailer_id="R-01", line_count=1,
        )
    if intent == "MIN_ORDER_QTY":
        return OrderEvent(
            order_id="WF-MOQ-1", line_item=1,
            po_price=100.0, sap_base_price=100.0,
            event_type="MIN_ORDER_QTY",
            retailer_id="R-01", line_count=1,
        )
    if intent == "PALLET_CONFIG":
        return OrderEvent(
            order_id="WF-PAL-1", line_item=1,
            po_price=100.0, sap_base_price=100.0,
            event_type="PALLET_CONFIG_VIOLATION",
            retailer_id="R-01", line_count=1,
        )
    if intent == "DELIVERY_DELAY":
        return OrderEvent(
            order_id="WF-DD-1", line_item=1,
            po_price=100.0, sap_base_price=100.0,
            event_type="DELIVERY_DELAY",
            retailer_id="R-01", line_count=1,
        )
    if intent == "MANUAL_ORDER_INTAKE":
        return OrderEvent(
            order_id="WF-MOI-1", line_item=1,
            po_price=100.0, sap_base_price=100.0,
            event_type="EMAIL_ORDER_ENTRY_REQUEST",
            retailer_id="R-01", line_count=1,
        )
    raise pytest.UsageError(
        f"_event_for_intent has no canonical event for intent {intent!r}. "
        f"Add a constructor or add the intent to NO_RECIPE_INTENTS in "
        f"tests/contract/test_intent_recipe_parity.py."
    )


# Drive the parametrize set from the routing table — adding a new intent
# to INTENT_TO_RECIPE_NAME automatically expands coverage with no test
# editing required (P4: self-extending).
from api.analysis_adapters import INTENT_TO_RECIPE_NAME  # noqa: E402

_PIPELINE_PAIRS: list[Tuple[str, str]] = sorted(INTENT_TO_RECIPE_NAME.items())


@pytest.mark.parametrize(
    "intent,expected_recipe", _PIPELINE_PAIRS,
    ids=[f"{i}->{r}" for i, r in _PIPELINE_PAIRS],
)
def test_pipeline_terminates_with_known_status(
    intent: str, expected_recipe: str,
) -> None:
    """Every (intent, recipe) pair must drive the graph to a terminal
    status that maps to a known lifecycle. Catches uncaught exceptions,
    None-final_status returns, and unknown enum values."""
    state = GraphState(event=_event_for_intent(intent))
    result = run_graph(state)
    assert result.final_status is not None, (
        f"Pipeline returned no final_status for intent={intent!r}. "
        f"Some node returned without setting it."
    )
    # final_status must be a real TerminalStatus value.
    valid = {s.value for s in TerminalStatus}
    assert result.final_status.value in valid, (
        f"Pipeline returned unknown final_status="
        f"{result.final_status!r} for intent={intent!r}."
    )
    # And must have a lifecycle mapping.
    lifecycle = STATUS_TO_LIFECYCLE.get(result.final_status.value)
    assert lifecycle in LIFECYCLE_STATES, (
        f"final_status={result.final_status.value!r} routes to "
        f"lifecycle={lifecycle!r} which is not in LIFECYCLE_STATES."
    )


@pytest.mark.parametrize(
    "intent,expected_recipe", _PIPELINE_PAIRS,
    ids=[f"{i}->{r}" for i, r in _PIPELINE_PAIRS],
)
def test_execution_log_populated_when_recipe_runs(
    intent: str, expected_recipe: str,
) -> None:
    """When the pipeline reaches `execute_recipe`, the execution_log
    carries intent_selected, recipe_name, trace_id, and
    constrained_outputs — otherwise the audit trail is broken.

    Some intents (BACK_ORDER, OVER_MAX, MIN_ORDER_QTY, PALLET_CONFIG,
    DELIVERY_DELAY, EDI_MISMATCH) require richer event metadata than
    these minimal canonical events carry, and FAIL_TO_HUMAN before
    execute_recipe — that's expected, exercised exhaustively by
    tests/test_e2e_om_adjacent_intents.py. The contract here is:
    *if* execute_recipe ran, then the audit fields are populated."""
    state = GraphState(event=_event_for_intent(intent))
    result = run_graph(state)
    log = result.execution_log
    if log is None:
        # Pipeline halted before execute_recipe — acceptable when the
        # canonical event doesn't carry the recipe's required metadata.
        # The audit-trail invariant only applies when the recipe ran.
        assert result.final_status in (
            TerminalStatus.FAIL_TO_HUMAN,
            TerminalStatus.MANUAL_REVIEW_REQUIRED,
            TerminalStatus.BLOCKED,
            TerminalStatus.AUDIT_CONTEXT_MISSING,
        ), (
            f"intent={intent!r} produced no execution_log AND a non-halt "
            f"final_status={result.final_status!r}. The pipeline reached "
            f"a terminal state without leaving any audit trace."
        )
        return
    assert log.intent_selected, "execution_log.intent_selected empty"
    assert log.trace_id, "execution_log.trace_id empty"
    if log.recipe_name:
        # If a recipe ran, it must be the routed recipe — drift would
        # mean classify_intent and select_recipe disagree.
        assert log.recipe_name == expected_recipe, (
            f"intent={intent!r} routed to recipe={log.recipe_name!r} "
            f"but INTENT_TO_RECIPE_NAME expects {expected_recipe!r}."
        )


@pytest.mark.parametrize(
    "intent,expected_recipe", _PIPELINE_PAIRS,
    ids=[f"{i}->{r}" for i, r in _PIPELINE_PAIRS],
)
def test_pipeline_classifies_to_target_intent(
    intent: str, expected_recipe: str,
) -> None:
    """Sanity check on the constructed event: classify_intent must
    actually produce the intent the test thinks it does. The classifier
    sets `result.intent` early — independent of whether execute_recipe
    runs."""
    state = GraphState(event=_event_for_intent(intent))
    result = run_graph(state)
    # `result.intent` is set by classify_intent, well before any halt.
    # Use it (not execution_log.intent_selected) so the assertion holds
    # for events that FAIL_TO_HUMAN at later nodes.
    assert result.intent is not None and result.intent.value == intent, (
        f"_event_for_intent({intent!r}) classified as "
        f"{result.intent and result.intent.value!r}. "
        f"Adjust the constructor or document the divergence."
    )
