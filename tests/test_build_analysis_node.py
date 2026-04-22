"""Pillar 2.1 tests — build_analysis enforces registry coverage on
every terminal path.

Invariants:
  * GREEN-path PHR record → coverage complete → state passes through
    unchanged.
  * Registry-missing adapter → build_analysis is a no-op (doesn't
    upgrade final_status, doesn't rewrite explanation).
  * State where the composer flags AUDIT_CONTEXT_MISSING → node
    upgrades final_status and appends a structured missing-fields
    breakdown to the explanation.
  * Re-entry guard: if state is already AUDIT_CONTEXT_MISSING,
    build_analysis is idempotent.
"""

from __future__ import annotations

from contracts.models import (
    ExecutionLog,
    GraphState,
    Intent,
    OrderEvent,
    TerminalStatus,
)
from orchestration.nodes import build_analysis


def _phr_event(po: float = 101.0) -> OrderEvent:
    return OrderEvent(
        order_id="ORD-BA-1",
        line_item=1,
        po_price=po,
        sap_base_price=100.0,
        event_type="EDI_850_PRICE_HOLD",
        metadata={"price_hold_status": "HELD"},
    )


def _phr_green_state() -> GraphState:
    """State after a normal GREEN run — recipe executed, outputs
    populated, no missing fields."""
    state = GraphState(event=_phr_event(po=101.0))
    state.intent = Intent.PRICE_HOLD_RELEASE
    state.selected_recipe = "PriceHoldReleaseRecipe.py"
    state.final_status = TerminalStatus.COMPLETE
    state.execution_log = ExecutionLog(
        trace_id="tr-1",
        recipe_name="PriceHoldReleaseRecipe.py",
        outputs={
            "status": "RELEASED",
            "action": "AUTO_RELEASE",
            "variance_pct": 0.01,
            "reason": "Variance within tolerance.",
            "order_id": "ORD-BA-1",
        },
    )
    return state


class TestBuildAnalysisHappyPath:
    def test_green_state_passes_through_unchanged(self):
        state = _phr_green_state()
        original_status = state.final_status
        original_explanation = state.explanation
        result = build_analysis(state)
        assert result is state  # mutates in place, same reference
        assert result.final_status == original_status  # still COMPLETE
        assert result.explanation == original_explanation  # untouched

    def test_record_outside_registry_is_noop(self):
        """CREDIT_BLOCK → CreditHoldReleaseRecipe has no AnalysisData
        section in the registry (and no adapter), so build_analysis
        finds nothing to enforce and is a no-op. Was previously
        PriceAdjustmentRecipe — re-pointed at T4 because that recipe
        now has adapt_price wired."""
        state = GraphState(event=_phr_event())
        state.intent = Intent.CREDIT_BLOCK
        state.selected_recipe = "CreditHoldReleaseRecipe.py"
        state.final_status = TerminalStatus.COMPLETE
        result = build_analysis(state)
        assert result.final_status == TerminalStatus.COMPLETE
        # No explanation mutation either — composer returned empty.


class TestBuildAnalysisEnforcement:
    def test_missing_audit_fields_emit_terminal(self):
        """Force the adapter to produce nothing (sap_base_price <= 0)
        → every enforced PHR field is missing → final_status flips."""
        event = OrderEvent(
            order_id="ORD-BAD",
            line_item=1,
            po_price=101.0,
            sap_base_price=0.0,  # breaks the adapter
            event_type="EDI_850_PRICE_HOLD",
            metadata={"price_hold_status": "HELD"},
        )
        state = GraphState(event=event)
        state.intent = Intent.PRICE_HOLD_RELEASE
        state.selected_recipe = "PriceHoldReleaseRecipe.py"
        state.final_status = TerminalStatus.COMPLETE
        state.execution_log = ExecutionLog(
            trace_id="tr-2",
            recipe_name="PriceHoldReleaseRecipe.py",
            outputs={"status": "FAILED"},
        )

        result = build_analysis(state)

        assert result.final_status == TerminalStatus.AUDIT_CONTEXT_MISSING
        assert result.explanation is not None
        assert "PriceHoldAnalysisData" in result.explanation
        # The missing-fields list is surfaced so the trace is actionable.
        assert "variance_pct" in result.explanation
        assert "tolerance_pct" in result.explanation

    def test_preserves_prior_explanation_as_preamble(self):
        """If a prior node set an explanation (e.g. shadow block),
        build_analysis appends its suffix rather than overwriting."""
        event = OrderEvent(
            order_id="ORD-PRE",
            line_item=1,
            po_price=101.0,
            sap_base_price=0.0,
            event_type="EDI_850_PRICE_HOLD",
            metadata={"price_hold_status": "HELD"},
        )
        state = GraphState(event=event)
        state.intent = Intent.PRICE_HOLD_RELEASE
        state.selected_recipe = "PriceHoldReleaseRecipe.py"
        state.final_status = TerminalStatus.COMPLETE
        state.explanation = "Shadow said GREEN and recipe ran OK."

        result = build_analysis(state)

        assert result.explanation.startswith("Shadow said GREEN")
        assert "Audit-bearing fields missing" in result.explanation

    def test_re_entry_guard_is_idempotent(self):
        """Calling build_analysis on a state already marked
        AUDIT_CONTEXT_MISSING must not double-append the missing-fields
        suffix. Cheap safety net."""
        event = OrderEvent(
            order_id="ORD-RE",
            line_item=1,
            po_price=101.0,
            sap_base_price=0.0,
            event_type="EDI_850_PRICE_HOLD",
            metadata={"price_hold_status": "HELD"},
        )
        state = GraphState(event=event)
        state.intent = Intent.PRICE_HOLD_RELEASE
        state.selected_recipe = "PriceHoldReleaseRecipe.py"
        state.final_status = TerminalStatus.AUDIT_CONTEXT_MISSING
        state.explanation = "Already flagged once."

        result = build_analysis(state)
        assert result.final_status == TerminalStatus.AUDIT_CONTEXT_MISSING
        assert result.explanation == "Already flagged once."
