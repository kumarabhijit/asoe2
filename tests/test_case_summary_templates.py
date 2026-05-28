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

    TODO_INTENTS = (
        "PRICE_DISCREPANCY",
        "BACK_ORDER",
        "CREDIT_BLOCK",
        "CONTRACTUAL_CORRECTION",
        "MASS_PRICING_ERROR",
        "OVER_MAX",
        "MOQ_UPLIFT",
        "DELIVERY_DELAY",
        "CHANGE_ANALYSIS",
    )

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
