"""ADR-041 P3e Phase 0b T2b — case_summary_templates tests.

Covers:
  * Registry-level dispatch (None record / unknown intent / no
    template / template returns None).
  * Implemented templates (DUPLICATE_PO, MANUAL_ORDER_INTAKE) —
    happy path + missing-field None paths.
  * Grandfather-clause intents (PRICE_HOLD, EDI_MISMATCH, PALLET,
    EMAIL_COMPLAINT) — registered with the explicit no-op sentinel.
  * TODO stubs return None (regression guard so a half-written
    template can't ship a partial-truth one-liner).

The end-to-end integration through `compute_case_summary` is
covered in `test_case_summary.py`; this file focuses on the
template layer in isolation.
"""

from __future__ import annotations

import pytest

from api.case_summary_templates import (
    INTENT_TEMPLATES,
    RenderedTemplate,
    _grandfathered_no_template,
    render_template,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StubRecord:
    """Minimal duck-typed stand-in for `ChildCase`. Templates touch
    only `intent`, `resolution_data`, `enrichment_context` —
    nothing else."""

    def __init__(
        self,
        *,
        intent: str | None = None,
        resolution_data: dict | None = None,
        enrichment_context: dict | None = None,
    ):
        self.intent = intent
        self.resolution_data = resolution_data or {}
        self.enrichment_context = enrichment_context or {}


class _StubCase:
    """Minimal duck-typed stand-in for `OrderCase`."""

    def __init__(self, case_id: str = "case-1", customer_id: str = "acme"):
        self.case_id = case_id
        self.customer_id = customer_id


# ---------------------------------------------------------------------------
# Dispatcher behaviour
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_none_record_returns_empty_template(self):
        result = render_template(None, _StubCase())
        assert result == RenderedTemplate()

    def test_record_with_no_intent_returns_empty(self):
        result = render_template(_StubRecord(intent=None), _StubCase())
        assert result == RenderedTemplate()

    def test_unrecognised_intent_returns_empty(self):
        """A new intent the Recipe team hasn't added yet falls
        through to all-None — never raises, never synthesises."""
        result = render_template(
            _StubRecord(intent="BRAND_NEW_UNKNOWN_INTENT"),
            _StubCase(),
        )
        assert result == RenderedTemplate()

    def test_template_returning_none_is_normalised_to_empty(self):
        """The dispatcher must never propagate None — the wire shape
        is `RenderedTemplate` (with None fields), not Optional."""
        # PRICE_DISCREPANCY's stub returns None today.
        result = render_template(
            _StubRecord(intent="PRICE_DISCREPANCY"),
            _StubCase(),
        )
        assert result == RenderedTemplate()
        # Confirm it's not None — the dispatcher's job.
        assert isinstance(result, RenderedTemplate)


# ---------------------------------------------------------------------------
# DUPLICATE_PO — working template
# ---------------------------------------------------------------------------


class TestDuplicatePOTemplate:
    def test_happy_path_renders_one_liner(self):
        record = _StubRecord(
            intent="DUPLICATE_PO",
            resolution_data={
                "duplicate_detection": {
                    "duplicate_order": {"po_number": "PO-2025-1042"},
                    "original_order": {"po_number": "PO-2025-0938"},
                    "days_between": 3,
                    "confidence": 0.92,
                },
            },
        )
        result = render_template(record, _StubCase())
        assert result.one_liner == (
            "PO PO-2025-1042 — duplicate of PO-2025-0938 (3d apart, 92% match)"
        )
        # SKU + title not produced for DUPLICATE_PO by spec.
        assert result.sku_code is None
        assert result.sku_title is None

    def test_missing_field_returns_none(self):
        """Recipe mid-classification — confidence absent. Template
        must not synthesise; return None and the dispatcher
        normalises to empty."""
        record = _StubRecord(
            intent="DUPLICATE_PO",
            resolution_data={
                "duplicate_detection": {
                    "duplicate_order": {"po_number": "PO-1"},
                    "original_order": {"po_number": "PO-2"},
                    "days_between": 3,
                    # confidence intentionally absent
                },
            },
        )
        result = render_template(record, _StubCase())
        assert result == RenderedTemplate()

    def test_no_detection_block_returns_none(self):
        """Record carries the DUPLICATE_PO intent but the recipe
        hasn't written its detection block yet."""
        record = _StubRecord(intent="DUPLICATE_PO", resolution_data={})
        result = render_template(record, _StubCase())
        assert result == RenderedTemplate()

    def test_confidence_round_matches_recipe_sme_spec(self):
        """`{confidence}% match` — the spec stores confidence as a
        0-1 float; the rendered string is the rounded percent."""
        record = _StubRecord(
            intent="DUPLICATE_PO",
            resolution_data={
                "duplicate_detection": {
                    "duplicate_order": {"po_number": "P1"},
                    "original_order": {"po_number": "P2"},
                    "days_between": 1,
                    "confidence": 0.876,
                },
            },
        )
        result = render_template(record, _StubCase())
        assert "88% match" in (result.one_liner or "")


# ---------------------------------------------------------------------------
# MANUAL_ORDER_INTAKE — working template
# ---------------------------------------------------------------------------


class TestManualOrderIntakeTemplate:
    def test_happy_path_renders_subject_with_classification(self):
        record = _StubRecord(
            intent="MANUAL_ORDER_INTAKE",
            enrichment_context={
                "email_source": {
                    "subject": "Order increase for PO-2026-0062",
                    "classification": "ORDER_CHANGE",
                },
            },
        )
        result = render_template(record, _StubCase())
        assert result.one_liner == (
            "Order Change: Order increase for PO-2026-0062"
        )

    def test_long_subject_is_truncated(self):
        """80-char ceiling + ellipsis — the row's line 3 only has
        so much horizontal space at 360px."""
        long_subject = "A" * 200
        record = _StubRecord(
            intent="MANUAL_ORDER_INTAKE",
            enrichment_context={
                "email_source": {
                    "subject": long_subject,
                    "classification": "INQUIRY",
                },
            },
        )
        result = render_template(record, _StubCase())
        assert result.one_liner is not None
        assert "…" in result.one_liner
        # Truncated subject + tag + separator < total length
        assert len(result.one_liner) < 200

    def test_missing_subject_returns_none(self):
        """Preprod fallback path — email_source enrichment absent."""
        record = _StubRecord(
            intent="MANUAL_ORDER_INTAKE",
            enrichment_context={},
        )
        result = render_template(record, _StubCase())
        assert result == RenderedTemplate()

    def test_missing_classification_falls_back_to_other(self):
        record = _StubRecord(
            intent="MANUAL_ORDER_INTAKE",
            enrichment_context={
                "email_source": {"subject": "Hi"},
            },
        )
        result = render_template(record, _StubCase())
        assert (result.one_liner or "").startswith("Other: ")


# ---------------------------------------------------------------------------
# PRICE_DISCREPANCY — Round 2 implementation
# ---------------------------------------------------------------------------


def _price_record(**overrides):
    """A ChildCase-like stub with the four source bags adapt_price
    consumes. Defaults pass the audit-bearing gateway gate."""
    event = {
        "po_price": 10.50,
        "sap_base_price": 9.80,
        "sku": "BEV-COLA-12PK",
        "line_count": 1,
        "uom": "EA",
        "metadata": {
            "total_quantity": 480,
            "material_desc": "Cola 12-pack",
        },
    }
    event.update(overrides.pop("event", {}))
    enrichment = {
        "sap_doc_context": {
            "doc_type": "ZOR",
            "doc_number": "5012345",
        },
        "contract_context": {
            "rule_id": "RULE-PRICE-001",
            "root_cause_category": "PROMO_EXPIRED",
        },
        "promotion_context": {
            "root_cause_category": "PROMO_EXPIRED",
        },
    }
    return _StubRecord(
        intent="PRICE_DISCREPANCY",
        resolution_data=overrides.get("resolution_data", {}),
        enrichment_context=enrichment,
    )._with_event(event)


# Extend _StubRecord with original_event support since adapters
# read from it (the existing stub didn't expose it).
def _stub_with_event(self, event):
    self.original_event = event
    return self


_StubRecord._with_event = _stub_with_event  # type: ignore[attr-defined]


class TestPriceDiscrepancyTemplate:
    def test_happy_path_renders_sku_and_one_liner(self):
        record = _price_record()
        result = render_template(record, _StubCase())
        assert result.sku_code == "BEV-COLA-12PK"
        assert result.sku_title == "Cola 12-pack"
        # The one-liner should mention prices + variance + at-risk.
        assert result.one_liner is not None
        assert "$10.50/EA" in result.one_liner
        assert "$9.80" in result.one_liner
        assert "%" in result.one_liner

    def test_missing_gateway_context_returns_empty(self):
        # adapt_price returns None when audit-bearing fields absent.
        record = _StubRecord(
            intent="PRICE_DISCREPANCY",
            enrichment_context={},  # no sap_doc / contract / promo
        )
        record.original_event = {"po_price": 10, "sap_base_price": 9}
        result = render_template(record, _StubCase())
        assert result == RenderedTemplate()

    def test_renders_without_material_desc(self):
        """material_desc is contextual; absence is OK."""
        record = _price_record()
        record.original_event["metadata"].pop("material_desc")
        result = render_template(record, _StubCase())
        assert result.sku_code == "BEV-COLA-12PK"
        assert result.sku_title is None  # gracefully absent
        assert result.one_liner is not None


# ---------------------------------------------------------------------------
# BACK_ORDER — Round 2 implementation
# ---------------------------------------------------------------------------


def _back_order_record(**overrides):
    # adapt_back_order reads quantities from event.metadata.*
    # (recipe input convention); sku is at the top level since
    # the template pulls it from there directly.
    event = {
        "sku": "BEV-COLA-12PK",
        "metadata": {
            "ordered_qty": 480,
            "available_qty": 380,
            "unit_price": 9.80,
            "uom": "EA",
        },
    }
    event.update(overrides.pop("event", {}))
    enrichment = {
        "inventory_snapshot": {
            "primary_dc": {
                "plant": "DC-100",
                "name": "Northeast DC",
                "region": "NE",
                "qty": 380,
            },
            "atp_date": "2026-06-04",
            "alternate_warehouses": [],
            "substitutes": [],
            "production": None,
            "inbound_po": None,
        },
    }
    # `recommended_action` present so adapt_back_order uses the
    # recipe-computed values rather than calling
    # _synthesize_back_order_outputs (which returns a fractional
    # gap_pct via the default-compute path — a separate
    # adapter-side concern outside this template's scope).
    rd = {
        "gap_qty": 100,
        "gap_pct": 20.8,
        "at_risk": 980.0,
        "recommended_action": "RESCHEDULE",
        "resolution_options": [],
    }
    rd.update(overrides.pop("resolution_data", {}))
    r = _StubRecord(
        intent="BACK_ORDER",
        resolution_data=rd,
        enrichment_context=enrichment,
    )
    r.original_event = event
    return r


class TestBackOrderTemplate:
    def test_happy_path_renders_sku_and_short_stock_line(self):
        record = _back_order_record()
        result = render_template(record, _StubCase())
        assert result.sku_code == "BEV-COLA-12PK"
        assert result.one_liner is not None
        assert "380/480" in result.one_liner
        assert "20.8% gap" in result.one_liner
        assert "ATP 2026-06-04" in result.one_liner

    def test_missing_inventory_snapshot_returns_empty(self):
        record = _back_order_record()
        record.enrichment_context = {}  # adapter returns None
        result = render_template(record, _StubCase())
        assert result == RenderedTemplate()


# ---------------------------------------------------------------------------
# CHANGE_ANALYSIS — Round 2 implementation
# ---------------------------------------------------------------------------


def _change_record(*, with_revenue: bool = True):
    enrichment = {
        "change_analysis": {
            "evaluation": {
                "lifecycle_stages": ["DRAFT", "REVIEW", "APPROVED"],
                "lifecycle_index": 1,
                "change_items": [
                    {
                        "field": "quantity",
                        "from_value": "100",
                        "to_value": "150",
                    },
                ],
                "checks": [],
                "pass_count": 0,
                "conditional_count": 0,
                "warning_count": 0,
            },
            "scenarios": [],
            "decision": {
                "recommended_action": "APPROVE_WITH_COSIGN",
                "confidence": 0.86,
                "rationale": "Within tolerance.",
                "revenue_impact_usd": 4200.0 if with_revenue else None,
                "requires_cosign": True,
                "sap_actions": [],
            },
        },
    }
    return _StubRecord(intent="CHANGE_ANALYSIS", enrichment_context=enrichment)


class TestChangeAnalysisTemplate:
    def test_happy_path_renders_field_arrow_action_revenue(self):
        result = render_template(_change_record(), _StubCase())
        assert result.one_liner is not None
        assert "quantity:" in result.one_liner
        assert "100 → 150" in result.one_liner
        assert "APPROVE_WITH_COSIGN" in result.one_liner
        assert "$4,200" in result.one_liner

    def test_no_revenue_impact_omits_suffix(self):
        result = render_template(_change_record(with_revenue=False), _StubCase())
        assert result.one_liner is not None
        # No dollar suffix when revenue_impact_usd is None.
        assert "$" not in result.one_liner

    def test_missing_change_analysis_returns_empty(self):
        record = _StubRecord(intent="CHANGE_ANALYSIS", enrichment_context={})
        result = render_template(record, _StubCase())
        assert result == RenderedTemplate()

    def test_empty_change_items_returns_empty(self):
        record = _change_record()
        record.enrichment_context["change_analysis"]["evaluation"][
            "change_items"
        ] = []
        result = render_template(record, _StubCase())
        assert result == RenderedTemplate()


# ---------------------------------------------------------------------------
# OVER_MAX — Round 2 implementation
# ---------------------------------------------------------------------------


def _over_max_record():
    event = {
        "order_id": "SO-OM-1",
        "metadata": {
            "total_ordered": 3000,
            "max_qty": 2760,
            "order_lines": [
                {
                    "sku": "BEV-COLA-12PK",
                    "description": "Cola 12-pack",
                    "qty": 3000,
                    "max_line_qty": 2760,
                    "excess": 240,
                },
            ],
        },
    }
    rd = {
        "status": "COMPLETE",
        "excess_qty": 240,
        "exceedance_pct": 8.7,
        "at_risk": 4080.0,
        "trim_plan": [
            {
                "sku": "BEV-COLA-12PK",
                "description": "Cola 12-pack",
                "qty_to_trim": 240,
                "complete_layer_aligned": True,
            },
        ],
    }
    r = _StubRecord(intent="OVER_MAX", resolution_data=rd)
    r.original_event = event
    return r


class TestOverMaxTemplate:
    def test_happy_path_picks_trim_plan_head_sku(self):
        result = render_template(_over_max_record(), _StubCase())
        assert result.sku_code == "BEV-COLA-12PK"
        assert result.sku_title == "Cola 12-pack"
        assert result.one_liner is not None
        assert "Over max by 240" in result.one_liner
        assert "8.7% over" in result.one_liner

    def test_missing_metadata_returns_empty(self):
        record = _over_max_record()
        record.original_event["metadata"] = {}
        result = render_template(record, _StubCase())
        assert result == RenderedTemplate()

    def test_multi_line_appends_more_suffix(self):
        """Phase 0b T2e — when trim_plan covers more than one
        line, sku_title gets `+N more` appended."""
        record = _over_max_record()
        record.resolution_data["trim_plan"].extend([
            {"sku": "SNK-CHIPS-FAM", "description": "Family chips",
             "qty_to_trim": 80, "complete_layer_aligned": True},
            {"sku": "BEV-WATER-24PK", "description": "Water 24pk",
             "qty_to_trim": 40, "complete_layer_aligned": True},
        ])
        result = render_template(record, _StubCase())
        # Head SKU stays the first line's value.
        assert result.sku_code == "BEV-COLA-12PK"
        assert result.sku_title is not None
        assert "+2 more" in result.sku_title


# ---------------------------------------------------------------------------
# MOQ_UPLIFT — Round 2 implementation
# ---------------------------------------------------------------------------


def _moq_record():
    event = {
        "order_id": "SO-MOQ-1",
        "metadata": {
            "ordered_qty": 64,
            "moq_qty": 100,
            "unit_cost": 9.80,
            "uom": "EA",
            "sku": "BEV-COLA-12PK",
            "description": "Cola 12-pack",
        },
    }
    rd = {
        "status": "COMPLETE",
        "shortfall_qty": 36,
        "shortfall_pct": 36.0,
        "at_risk": 352.80,
    }
    r = _StubRecord(intent="MOQ_UPLIFT", resolution_data=rd)
    r.original_event = event
    return r


class TestMoqUpliftTemplate:
    def test_happy_path_renders_sku_and_shortfall(self):
        result = render_template(_moq_record(), _StubCase())
        assert result.sku_code == "BEV-COLA-12PK"
        assert result.one_liner is not None
        assert "Below MOQ by 36 EA" in result.one_liner
        assert "ordered 64 vs 100 required" in result.one_liner

    def test_missing_metadata_returns_empty(self):
        record = _moq_record()
        record.original_event["metadata"] = {}
        result = render_template(record, _StubCase())
        assert result == RenderedTemplate()

    def test_single_line_no_more_suffix(self):
        """MOQ_UPLIFT's adapter normalises round_up_plan to a
        single entry by design (one SKU per case). The Phase 0b
        T2e suffix wiring is in the template for symmetry but
        never fires under today's adapter shape."""
        result = render_template(_moq_record(), _StubCase())
        assert result.sku_title is not None
        # No multi-line suffix on the single-SKU recipe surface.
        assert "+" not in result.sku_title


# Phase 0b T2e — _with_more_suffix helper unit tests.
class TestWithMoreSuffix:
    def test_single_line_returns_title_unchanged(self):
        from api.case_summary_templates import _with_more_suffix

        assert _with_more_suffix("Cola 12-pack", 1) == "Cola 12-pack"

    def test_zero_or_negative_plan_returns_title_unchanged(self):
        from api.case_summary_templates import _with_more_suffix

        assert _with_more_suffix("Cola", 0) == "Cola"
        assert _with_more_suffix("Cola", -1) == "Cola"

    def test_multi_line_appends_suffix(self):
        from api.case_summary_templates import _with_more_suffix

        assert _with_more_suffix("Cola", 3) == "Cola (+2 more)"

    def test_no_title_renders_suffix_alone_for_multi_line(self):
        from api.case_summary_templates import _with_more_suffix

        # When the recipe gives us a count but no description,
        # surface the count alone so the operator still sees the
        # multi-line indicator.
        assert _with_more_suffix(None, 4) == "+3 more"

    def test_no_title_single_line_stays_none(self):
        from api.case_summary_templates import _with_more_suffix

        assert _with_more_suffix(None, 1) is None


# ---------------------------------------------------------------------------
# DELIVERY_DELAY — Round 2 implementation
# ---------------------------------------------------------------------------


def _delivery_delay_record(*, affected_lines: int = 3):
    event = {
        "order_id": "SO-DD-1",
        "line_count": affected_lines,
        "metadata": {
            "planned_date": "2026-05-30",
            "projected_eta": "2026-06-04",
            "carrier": "ACME-FREIGHT",
            "route": "NE-1",
        },
    }
    rd = {
        "status": "COMPLETE",
        "planned_date": "2026-05-30",
        "projected_eta": "2026-06-04",
        "days_late": 5,
        "delay_category": "MINOR",
        "affected_lines": affected_lines,
    }
    r = _StubRecord(intent="DELIVERY_DELAY", resolution_data=rd)
    r.original_event = event
    return r


class TestDeliveryDelayTemplate:
    def test_happy_path_plural_lines(self):
        result = render_template(_delivery_delay_record(), _StubCase())
        assert result.sku_code is None  # multi-line intent
        assert result.one_liner is not None
        assert "3 lines" in result.one_liner
        assert "5d late" in result.one_liner
        assert "MINOR" in result.one_liner
        assert "ETA 2026-06-04" in result.one_liner

    def test_singular_line(self):
        result = render_template(
            _delivery_delay_record(affected_lines=1), _StubCase(),
        )
        assert result.one_liner is not None
        assert "1 line " in result.one_liner

    def test_missing_event_dates_returns_empty(self):
        record = _delivery_delay_record()
        record.resolution_data = {}  # adapter falls through to event metadata
        record.original_event["metadata"] = {}
        result = render_template(record, _StubCase())
        assert result == RenderedTemplate()


# ---------------------------------------------------------------------------
# CREDIT_BLOCK — Round 2 implementation
# ---------------------------------------------------------------------------


class TestCreditBlockTemplate:
    def test_happy_path_renders_exposure_over_limit(self):
        record = _StubRecord(
            intent="CREDIT_BLOCK",
            enrichment_context={
                "customer_master_context": {
                    "credit_limit": 40000.0,
                    "current_exposure": 48200.0,
                },
            },
        )
        result = render_template(record, _StubCase())
        assert result.one_liner is not None
        assert "$48,200" in result.one_liner
        assert "$40,000" in result.one_liner
        assert "breached" in result.one_liner

    def test_missing_customer_master_context_returns_empty(self):
        record = _StubRecord(intent="CREDIT_BLOCK", enrichment_context={})
        result = render_template(record, _StubCase())
        assert result == RenderedTemplate()

    def test_partial_field_set_returns_empty(self):
        """Only credit_limit present → can't render the comparison."""
        record = _StubRecord(
            intent="CREDIT_BLOCK",
            enrichment_context={
                "customer_master_context": {"credit_limit": 40000.0},
            },
        )
        result = render_template(record, _StubCase())
        assert result == RenderedTemplate()

    def test_non_numeric_values_return_empty(self):
        record = _StubRecord(
            intent="CREDIT_BLOCK",
            enrichment_context={
                "customer_master_context": {
                    "credit_limit": "not a number",
                    "current_exposure": 100,
                },
            },
        )
        result = render_template(record, _StubCase())
        assert result == RenderedTemplate()


# ---------------------------------------------------------------------------
# CONTRACTUAL_CORRECTION — Round 2 implementation (delegates to PRICE)
# ---------------------------------------------------------------------------


class TestContractualCorrectionTemplate:
    def test_delegates_to_price_discrepancy(self):
        """Both intents route through PriceAdjustmentRecipe; the
        template re-uses the same adapter dispatch. A happy path
        for CONTRACTUAL_CORRECTION should produce the same shape
        as PRICE_DISCREPANCY."""
        record = _price_record()
        record.intent = "CONTRACTUAL_CORRECTION"
        result = render_template(record, _StubCase())
        # Same shape as PRICE_DISCREPANCY happy-path output.
        assert result.sku_code == "BEV-COLA-12PK"
        assert result.one_liner is not None
        assert "$10.50" in result.one_liner
        assert "$9.80" in result.one_liner

    def test_missing_gateway_context_returns_empty(self):
        record = _StubRecord(
            intent="CONTRACTUAL_CORRECTION",
            enrichment_context={},
        )
        record.original_event = {"po_price": 10}
        result = render_template(record, _StubCase())
        assert result == RenderedTemplate()


# ---------------------------------------------------------------------------
# MASS_PRICING_ERROR — Round 2 implementation
# ---------------------------------------------------------------------------


class TestMassPricingErrorTemplate:
    def test_happy_path_renders_line_count_and_avg_variance(self):
        record = _StubRecord(intent="MASS_PRICING_ERROR")
        record.original_event = {
            "line_count": 12,
            "metadata": {"avg_variance_pct": 8.2},
        }
        result = render_template(record, _StubCase())
        assert result.sku_code is None  # multi-SKU intent
        assert result.one_liner is not None
        assert "12 lines" in result.one_liner
        assert "8.2% variance" in result.one_liner

    def test_computes_variance_from_line_items_when_avg_absent(self):
        record = _StubRecord(intent="MASS_PRICING_ERROR")
        record.original_event = {
            "line_count": 2,
            "line_items": [
                {"po_price": 11.0, "sap_base_price": 10.0},  # 10% delta
                {"po_price": 9.0, "sap_base_price": 10.0},   # 10% delta
            ],
        }
        result = render_template(record, _StubCase())
        assert result.one_liner is not None
        assert "10.0% variance" in result.one_liner

    def test_no_signals_returns_empty(self):
        record = _StubRecord(intent="MASS_PRICING_ERROR")
        record.original_event = {}
        result = render_template(record, _StubCase())
        assert result == RenderedTemplate()

    def test_line_count_only_renders_without_variance_suffix(self):
        record = _StubRecord(intent="MASS_PRICING_ERROR")
        record.original_event = {"line_count": 7}
        result = render_template(record, _StubCase())
        assert result.one_liner == "Bulk price drift across 7 lines"


# ---------------------------------------------------------------------------
# Grandfather-clause registrations
# ---------------------------------------------------------------------------


class TestGrandfatheredIntents:
    """These four intents are intentionally registered with the
    no-op sentinel — distinct from the all-stub TODOs so the
    Recipe team can grep the registry to find them. Each returns
    None today because the recipe output is too sparse for an
    honest one-liner (per Recipe SME panel §3)."""

    @pytest.mark.parametrize(
        "intent",
        ["PRICE_HOLD", "EDI_MISMATCH", "PALLET", "EMAIL_COMPLAINT"],
    )
    def test_grandfathered_intent_renders_empty(self, intent):
        record = _StubRecord(intent=intent, resolution_data={"anything": "here"})
        result = render_template(record, _StubCase())
        assert result == RenderedTemplate()

    @pytest.mark.parametrize(
        "intent",
        ["PRICE_HOLD", "EDI_MISMATCH", "PALLET", "EMAIL_COMPLAINT"],
    )
    def test_grandfathered_intent_uses_explicit_sentinel(self, intent):
        """Distinct symbol identifies the deliberate no-op vs. a
        missing registration. The architectural-lock test can
        assert the four expected intents wire to the sentinel."""
        assert INTENT_TEMPLATES[intent] is _grandfathered_no_template


# ---------------------------------------------------------------------------
# TODO stubs — regression guard
# ---------------------------------------------------------------------------


class TestTodoStubs:
    """Every TODO template stub MUST return None today. A
    half-written template that returns a partial RenderedTemplate
    would ship a misleading one-liner to operators; this lock
    catches that regression at the test level."""

    TODO_INTENTS: tuple = ()  # all 9 templates implemented in Round 2

    @pytest.mark.parametrize("intent", TODO_INTENTS)
    def test_todo_template_returns_empty(self, intent):
        """When the Recipe team lands a working template, this
        test's expectation flips — the test then asserts the new
        behaviour. Until then, an accidentally-half-written stub
        breaks here."""
        record = _StubRecord(
            intent=intent,
            resolution_data={
                # Provide a bunch of fields so a half-written
                # template that reads any of them gets a value;
                # the test catches that the template still returns
                # None.
                "price_analysis": {"sku": "X", "variance_pct": 5.0},
                "backorder_analysis": {"available_qty": 100},
                "overmax_analysis": {"excess_qty": 50},
                "moq_analysis": {"shortfall_qty": 20},
                "delivery_delay_analysis": {"days_late": 3},
                "change_analysis": {"change_items": [{"field": "qty"}]},
            },
        )
        result = render_template(record, _StubCase())
        assert result == RenderedTemplate(), (
            f"{intent} template returned non-empty before Recipe team "
            "landed the full implementation — flip this test's "
            "expectation when the working template ships."
        )


# ---------------------------------------------------------------------------
# Registry-coverage lock
# ---------------------------------------------------------------------------


class TestRegistryCoverage:
    def test_registry_covers_all_panel_intents(self):
        """The 15 intents the 2026-05-28 Recipe SME panel mapped
        must all be in the registry — either with a working
        template, a TODO stub, or the grandfather-clause sentinel.
        New intents from the backend MUST be added explicitly so
        the row's per-intent shape is intentional, not accidental."""
        EXPECTED = {
            "DUPLICATE_PO",
            "MANUAL_ORDER_INTAKE",
            "PRICE_HOLD",
            "EDI_MISMATCH",
            "PALLET",
            "EMAIL_COMPLAINT",
            "PRICE_DISCREPANCY",
            "BACK_ORDER",
            "CREDIT_BLOCK",
            "CONTRACTUAL_CORRECTION",
            "MASS_PRICING_ERROR",
            "OVER_MAX",
            "MOQ_UPLIFT",
            "DELIVERY_DELAY",
            "CHANGE_ANALYSIS",
        }
        missing = EXPECTED - INTENT_TEMPLATES.keys()
        assert not missing, (
            f"Registry missing intents from the Recipe SME panel "
            f"spec: {missing}. Add an entry to INTENT_TEMPLATES."
        )
