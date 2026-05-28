"""ADR-041 P3e Phase 0b T2c — verdict-color gate tests.

Eight rules from the Recipe SME panel §5; each gets a unit case.
The gate is a pure projection, so tests construct stub records
and assert the gated color matches the panel's expected
outcome.
"""

from __future__ import annotations

import pytest

from api.case_summary_verdict_gates import (
    DUPLICATE_PO_GREEN_FLOOR_USD,
    PRICE_HOLD_NEAR_CEILING_FRACTION,
    apply_verdict_color_gates,
)


class _Record:
    def __init__(self, *, resolution_data=None, enrichment_context=None):
        self.resolution_data = resolution_data or {}
        self.enrichment_context = enrichment_context or {}


class _Case:
    def __init__(self, *, tier: int | None = None):
        self.tier = tier


# ---------------------------------------------------------------------------
# Pass-through behaviour
# ---------------------------------------------------------------------------


class TestPassThrough:
    def test_none_in_none_out(self):
        assert apply_verdict_color_gates(
            None, intent="DUPLICATE_PO", primary_record=_Record(), case=_Case(),
        ) is None

    def test_no_intent_passes_through(self):
        assert apply_verdict_color_gates(
            "R", intent=None, primary_record=_Record(), case=_Case(),
        ) == "R"

    def test_no_primary_record_passes_through(self):
        assert apply_verdict_color_gates(
            "R", intent="DUPLICATE_PO", primary_record=None, case=_Case(),
        ) == "R"

    def test_no_gate_applies_returns_raw(self):
        # Intent without a rule, no high-tier customer — passes
        # through.
        assert apply_verdict_color_gates(
            "G",
            intent="MANUAL_ORDER_INTAKE",
            primary_record=_Record(),
            case=_Case(tier=3),
        ) == "G"


# ---------------------------------------------------------------------------
# Rule 1 — DELIVERY_DELAY days_late > 0 but no SLA breach → CEILING AMBER
# ---------------------------------------------------------------------------


class TestDeliveryDelayCeiling:
    def test_days_late_without_sla_breach_lifts_red_to_amber(self):
        record = _Record(resolution_data={
            "delivery_delay_analysis": {"days_late": 5, "sla_breach": False},
        })
        gated = apply_verdict_color_gates(
            "R", intent="DELIVERY_DELAY",
            primary_record=record, case=_Case(),
        )
        assert gated == "A"

    def test_sla_breach_keeps_red(self):
        record = _Record(resolution_data={
            "delivery_delay_analysis": {"days_late": 5, "sla_breach": True},
        })
        gated = apply_verdict_color_gates(
            "R", intent="DELIVERY_DELAY",
            primary_record=record, case=_Case(),
        )
        assert gated == "R"

    def test_zero_days_late_keeps_red(self):
        """Defensive — RED with 0 days_late is something else
        entirely (recipe inconsistency); don't override."""
        record = _Record(resolution_data={
            "delivery_delay_analysis": {"days_late": 0},
        })
        gated = apply_verdict_color_gates(
            "R", intent="DELIVERY_DELAY",
            primary_record=record, case=_Case(),
        )
        assert gated == "R"


# ---------------------------------------------------------------------------
# Rule 2 — PALLET BROKEN_LAYER / PARTIAL_PALLET → CEILING AMBER
# ---------------------------------------------------------------------------


class TestPalletCeiling:
    @pytest.mark.parametrize("cls", ["BROKEN_LAYER", "PARTIAL_PALLET"])
    def test_routine_pallet_class_lifts_red_to_amber(self, cls):
        record = _Record(resolution_data={
            "pallet_analysis": {"classification": cls},
        })
        gated = apply_verdict_color_gates(
            "R", intent="PALLET",
            primary_record=record, case=_Case(),
        )
        assert gated == "A"

    def test_other_pallet_class_keeps_red(self):
        record = _Record(resolution_data={
            "pallet_analysis": {"classification": "MULTI_TIER_VIOLATION"},
        })
        gated = apply_verdict_color_gates(
            "R", intent="PALLET",
            primary_record=record, case=_Case(),
        )
        assert gated == "R"


# ---------------------------------------------------------------------------
# Rule 3 — EDI_MISMATCH routine subtypes → CEILING AMBER
# ---------------------------------------------------------------------------


class TestEdiMismatchCeiling:
    @pytest.mark.parametrize("sub", ["DATE_FORMAT", "UOM_NORMALISATION"])
    def test_routine_subtype_lifts_red_to_amber(self, sub):
        record = _Record(resolution_data={
            "edi_mismatch_analysis": {"sub_type": sub},
        })
        gated = apply_verdict_color_gates(
            "R", intent="EDI_MISMATCH",
            primary_record=record, case=_Case(),
        )
        assert gated == "A"

    def test_non_routine_subtype_keeps_red(self):
        record = _Record(resolution_data={
            "edi_mismatch_analysis": {"sub_type": "QTY_MISMATCH"},
        })
        gated = apply_verdict_color_gates(
            "R", intent="EDI_MISMATCH",
            primary_record=record, case=_Case(),
        )
        assert gated == "R"


# ---------------------------------------------------------------------------
# Rule 4 — PRICE_HOLD AUTO_RELEASE near ceiling → FLOOR AMBER
# ---------------------------------------------------------------------------


class TestPriceHoldFloor:
    def test_auto_release_near_ceiling_lifts_green_to_amber(self):
        # tolerance=10%, variance=9% → 9/10 = 0.9 >= 0.8 fraction.
        record = _Record(resolution_data={
            "price_hold_analysis": {
                "action": "AUTO_RELEASE",
                "variance_pct": 9.0,
                "tolerance_pct": 10.0,
            },
        })
        gated = apply_verdict_color_gates(
            "G", intent="PRICE_HOLD",
            primary_record=record, case=_Case(),
        )
        assert gated == "A"

    def test_well_within_tolerance_keeps_green(self):
        # variance=5%, tolerance=10% → 0.5 < 0.8 → no override.
        record = _Record(resolution_data={
            "price_hold_analysis": {
                "action": "AUTO_RELEASE",
                "variance_pct": 5.0,
                "tolerance_pct": 10.0,
            },
        })
        gated = apply_verdict_color_gates(
            "G", intent="PRICE_HOLD",
            primary_record=record, case=_Case(),
        )
        assert gated == "G"

    def test_non_auto_release_keeps_green(self):
        record = _Record(resolution_data={
            "price_hold_analysis": {
                "action": "ESCALATE",
                "variance_pct": 9.0,
                "tolerance_pct": 10.0,
            },
        })
        gated = apply_verdict_color_gates(
            "G", intent="PRICE_HOLD",
            primary_record=record, case=_Case(),
        )
        assert gated == "G"


# ---------------------------------------------------------------------------
# Rule 5 — DUPLICATE_PO > $10k → FLOOR AMBER
# ---------------------------------------------------------------------------


class TestDuplicatePoFloor:
    def test_above_floor_lifts_green_to_amber(self):
        record = _Record(resolution_data={
            "duplicate_detection": {
                "duplicate_order": {
                    "total_value": DUPLICATE_PO_GREEN_FLOOR_USD + 1,
                },
            },
        })
        gated = apply_verdict_color_gates(
            "G", intent="DUPLICATE_PO",
            primary_record=record, case=_Case(),
        )
        assert gated == "A"

    def test_at_floor_keeps_green(self):
        record = _Record(resolution_data={
            "duplicate_detection": {
                "duplicate_order": {
                    "total_value": DUPLICATE_PO_GREEN_FLOOR_USD,
                },
            },
        })
        gated = apply_verdict_color_gates(
            "G", intent="DUPLICATE_PO",
            primary_record=record, case=_Case(),
        )
        assert gated == "G"


# ---------------------------------------------------------------------------
# Rule 6 — Tier-1 customer (any intent) → FLOOR AMBER
# ---------------------------------------------------------------------------


class TestTier1CustomerFloor:
    def test_tier_1_lifts_green_to_amber(self):
        gated = apply_verdict_color_gates(
            "G", intent="MANUAL_ORDER_INTAKE",
            primary_record=_Record(), case=_Case(tier=1),
        )
        assert gated == "A"

    def test_tier_2_keeps_green(self):
        gated = apply_verdict_color_gates(
            "G", intent="MANUAL_ORDER_INTAKE",
            primary_record=_Record(), case=_Case(tier=2),
        )
        assert gated == "G"

    def test_tier_1_applies_across_intents(self):
        for intent in ("DUPLICATE_PO", "BACK_ORDER", "PRICE_DISCREPANCY"):
            gated = apply_verdict_color_gates(
                "G", intent=intent,
                primary_record=_Record(), case=_Case(tier=1),
            )
            assert gated == "A", f"tier-1 must floor {intent}"


# ---------------------------------------------------------------------------
# Rule 7 — Requires-cosign on EMAIL_COMPLAINT / CHANGE_ANALYSIS → FLOOR AMBER
# ---------------------------------------------------------------------------


class TestCosignFloor:
    @pytest.mark.parametrize("intent", ["EMAIL_COMPLAINT", "CHANGE_ANALYSIS"])
    def test_requires_cosign_lifts_green_to_amber(self, intent):
        record = _Record(enrichment_context={
            "change_analysis": {
                "decision": {"requires_cosign": True},
            },
        })
        gated = apply_verdict_color_gates(
            "G", intent=intent,
            primary_record=record, case=_Case(),
        )
        assert gated == "A"

    def test_no_cosign_required_keeps_green(self):
        record = _Record(enrichment_context={
            "change_analysis": {
                "decision": {"requires_cosign": False},
            },
        })
        gated = apply_verdict_color_gates(
            "G", intent="EMAIL_COMPLAINT",
            primary_record=record, case=_Case(),
        )
        assert gated == "G"

    def test_other_intents_unaffected_by_cosign_field(self):
        """Rule 7 applies only to EMAIL_COMPLAINT / CHANGE_ANALYSIS.
        A DUPLICATE_PO with requires_cosign=true doesn't trip this
        gate (it has its own dollar-floor rule)."""
        record = _Record(enrichment_context={
            "change_analysis": {
                "decision": {"requires_cosign": True},
            },
        })
        gated = apply_verdict_color_gates(
            "G", intent="MANUAL_ORDER_INTAKE",
            primary_record=record, case=_Case(),
        )
        assert gated == "G"


# ---------------------------------------------------------------------------
# Integration with compute_case_summary
# ---------------------------------------------------------------------------


def test_compute_case_summary_applies_gates():
    """End-to-end: compute_case_summary passes the raw rollup
    through apply_verdict_color_gates. A child with shadow_verdict=
    GREEN under a tier-1 customer must produce audit_verdict_color=
    'A' on the projection."""
    from api.case_summary import compute_case_summary

    class _ChildLike:
        def __init__(self):
            self.shadow_verdict = "GREEN"
            self.intent = "MANUAL_ORDER_INTAKE"
            self.resolution_data = {}
            self.enrichment_context = {}
            self.original_event = {}

    class _CaseLike:
        case_id = "case-x"
        customer_id = "acme-tier1"
        tier = 1

    summary = compute_case_summary(_CaseLike(), [_ChildLike()])
    assert summary.audit_verdict_color == "A", (
        "Tier-1 customer floor (Rule 6) must lift GREEN to AMBER"
    )
