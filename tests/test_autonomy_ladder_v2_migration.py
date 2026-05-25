"""ADR-042 §5 (item 3) — the autonomy *gating ladder* migrates to v2.

Background: autonomy v2 (the prototype ordering — L1 = most autonomous … L4 =
human escalation) first landed DISPLAY-ONLY: `/health` served the v2 labels but
the `contracts/policy.py` gating ladders that recipes dispatch on still emitted
v1 strings (L1 = observe … L4 = full autonomy). That left an incoherence —
"L1" the operator saw (v2) meant the opposite of "L1" the engine emitted (v1).

With the dual-control compliance sign-off done (waived-but-mechanism-intact in
this pre-prod project), the gating ladder is migrated to v2 so the operator
vocabulary and the engine vocabulary agree under one version.

The migration is **behaviour-preserving**: every resolution action keeps the
exact same *degree of automation* it had under v1 — only the string used to
name that degree flips from the v1 ladder to the v2 ladder (a mirror by rank:
v1 L1↔v2 L4, v1 L2↔v2 L3, …). No historical record is reinterpreted: v1 stays
intact in `contracts.autonomy`, and newly-created records stamp
`autonomy_vocab_version = "v2"` so an old (unstamped → v1) record always
resolves under its own vocabulary.
"""

from __future__ import annotations

from contracts.autonomy import CURRENT_AUTONOMY_VOCAB_VERSION, autonomy_rank
from contracts.models import GraphState, Intent, OrderEvent, RecipeInvocation
from contracts.policy import (
    DUPLICATE_PO_AUTONOMY_LEVELS,
    EDI_MISMATCH_AUTONOMY_LEVELS,
    MANUAL_ORDER_INTAKE_AUTONOMY_LEVELS,
)
from orchestration.nodes import execute_recipe

# The v1 ladders as they shipped (the degree-of-automation reference the
# migration must preserve). Sourced from the pre-migration contracts/policy.py.
_V1_DUPLICATE_PO = {
    "BLOCK_AND_NOTIFY": "L3", "MERGE": "L2", "SUPERSEDE": "L2",
    "ALLOW_BOTH": "L3", "ESCALATE": "L1", "REQUEST_BUYER_CONFIRMATION": "L2",
}
_V1_EDI_MISMATCH = {
    "SKU_MISMATCH": "L3", "QTY_MISMATCH": "L2",
    "UOM_MISMATCH": "L2", "SHIP_TO_MISMATCH": "L1",
}
_V1_MANUAL_ORDER_INTAKE = {
    "ONE_CLICK_APPROVE": "L3", "STANDARD_REVIEW": "L2", "LOW_CONFIDENCE_FLAG": "L1",
    "AUTO_CORRECT": "L3", "REQUEST_CLARIFICATION": "L2", "ESCALATE": "L1",
    "REJECT": "L1",
}


def _v1_to_v2(level: str) -> str:
    """The v2 level with the SAME degree of automation as the v1 ``level``."""
    rank = autonomy_rank(level, "v1")
    for candidate in ("L1", "L2", "L3", "L4"):
        if autonomy_rank(candidate, "v2") == rank:
            return candidate
    raise AssertionError(f"no v2 level mirrors v1 {level!r}")


class TestLadderMigratedToV2:
    """The production ladders now speak v2, preserving each action's automation."""

    def test_current_vocab_is_v2(self) -> None:
        assert CURRENT_AUTONOMY_VOCAB_VERSION == "v2"

    def test_duplicate_po_ladder_is_v2_behaviour_preserving(self) -> None:
        expected = {a: _v1_to_v2(lvl) for a, lvl in _V1_DUPLICATE_PO.items()}
        assert DUPLICATE_PO_AUTONOMY_LEVELS == expected

    def test_edi_mismatch_ladder_is_v2_behaviour_preserving(self) -> None:
        expected = {a: _v1_to_v2(lvl) for a, lvl in _V1_EDI_MISMATCH.items()}
        assert EDI_MISMATCH_AUTONOMY_LEVELS == expected

    def test_manual_order_intake_ladder_is_v2_behaviour_preserving(self) -> None:
        expected = {a: _v1_to_v2(lvl) for a, lvl in _V1_MANUAL_ORDER_INTAKE.items()}
        assert MANUAL_ORDER_INTAKE_AUTONOMY_LEVELS == expected

    def test_every_action_keeps_its_v1_degree_of_automation(self) -> None:
        """The dollar-safety property: an action's rank (degree of automation)
        is identical before and after the migration. A regression here would
        silently make a once-human-gated action auto-execute (or vice-versa)."""
        pairs = [
            (DUPLICATE_PO_AUTONOMY_LEVELS, _V1_DUPLICATE_PO),
            (EDI_MISMATCH_AUTONOMY_LEVELS, _V1_EDI_MISMATCH),
            (MANUAL_ORDER_INTAKE_AUTONOMY_LEVELS, _V1_MANUAL_ORDER_INTAKE),
        ]
        for v2_ladder, v1_ladder in pairs:
            assert set(v2_ladder) == set(v1_ladder)
            for action, v1_level in v1_ladder.items():
                assert autonomy_rank(v2_ladder[action], "v2") == autonomy_rank(
                    v1_level, "v1"
                ), action


class TestHistoricalRecordsKeepOriginalMeaning:
    """v1 is not mutated in place — old records resolve under their own vocab."""

    def test_v1_still_resolves_independently_of_v2(self) -> None:
        # The same level string means the OPPOSITE degree across versions.
        assert autonomy_rank("L1", "v1") == autonomy_rank("L4", "v2")
        assert autonomy_rank("L4", "v1") == autonomy_rank("L1", "v2")


def _dup_state(order_id: str, *, line_items_identical: bool) -> GraphState:
    from contracts.models import ComplianceDecision, ShadowStatus

    signals = {
        "po_number": 1.0, "customer_id": 1.0, "line_items": 1.0, "amount": 1.0,
        "timestamp": 1.0, "ship_to": 1.0, "channel": 1.0, "delivery_date": 1.0,
    }
    state = GraphState(event=OrderEvent(
        order_id=order_id, po_price=100.0, sap_base_price=100.0,
        event_type="EDI_850_DUPLICATE_PO", retailer_id="R-10",
        metadata={"signal_scores": signals},
    ))
    state.intent = Intent.DUPLICATE_PO
    state.shadow = ComplianceDecision(
        status=ShadowStatus.GREEN,
        reasons=["No blocking policy hit."],
        policy_hits=[],
        constrained_by="Guidance/Outlines fallback schema",
    )
    state.invocation = RecipeInvocation(
        recipe_name="DuplicatePORecipe.py",
        params={
            "incoming_po_number": order_id, "customer_id": "R-10",
            "signal_scores": signals, "threshold_auto_block": 0.90,
            "threshold_review_required": 0.70, "threshold_soft_flag": 0.50,
            "original_fulfilled": False, "has_revision_indicator": False,
            "line_items_identical": line_items_identical,
            "autonomy_levels": DUPLICATE_PO_AUTONOMY_LEVELS,
        },
    )
    return state


class TestGateDispatchesOnV2:
    """The execute_recipe autonomy gate routes by degree of automation, so the
    behaviour is unchanged even though the emitted strings flipped."""

    def test_low_autonomy_action_still_requires_human(self) -> None:
        from contracts.models import TerminalStatus

        # MERGE: v1 L2 → v2 L3 (rank 1, low autonomy) → human approval.
        result = execute_recipe(_dup_state("PO-V2-1", line_items_identical=False))
        assert result.execution_log.outputs["recommended_action"] == "MERGE"
        assert result.execution_log.outputs["autonomy_level"] == "L3"
        assert result.final_status == TerminalStatus.MANUAL_REVIEW_REQUIRED

    def test_high_autonomy_action_still_proceeds(self) -> None:
        from contracts.models import TerminalStatus

        # BLOCK_AND_NOTIFY: v1 L3 → v2 L2 (rank 2, high autonomy) → proceeds.
        result = execute_recipe(_dup_state("PO-V2-2", line_items_identical=True))
        assert result.execution_log.outputs["recommended_action"] == "BLOCK_AND_NOTIFY"
        assert result.execution_log.outputs["autonomy_level"] == "L2"
        assert result.final_status == TerminalStatus.BLOCKED

    def test_records_stamp_their_vocab_version(self) -> None:
        result = execute_recipe(_dup_state("PO-V2-3", line_items_identical=True))
        outputs = result.execution_log.outputs
        assert outputs.get("autonomy_level") is not None
        assert outputs["autonomy_vocab_version"] == CURRENT_AUTONOMY_VOCAB_VERSION
