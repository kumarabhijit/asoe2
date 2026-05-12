"""ADR-034 §6.2 — event_type alias compatibility (S14).

Locks the transitional dual-acceptance posture defined in
§6.2: both `EMAIL_ORDER_ENTRY_REQUEST` (legacy) and
`MANUAL_ORDER_INTAKE` (canonical) resolve to the same intent
classification, the same recipe selection, and the same
autonomy-level mapping. Producers may emit either name until
the 2026-08-12 deadline.

When the deadline passes, the test flips polarity: the legacy
name must 400 at the API boundary. That flip lands in the same
PR that retires §6.2 dual acceptance — this file's expectations
are then inverted and the deprecation-counter test (test_deprecated_event_type_metric.py)
is retired with a link back to §6.2.
"""

from __future__ import annotations

import pytest

from agents.harness import ROUTABLE_EVENT_TYPES
from api.case_resolver import _MANUAL_EVENT_TYPES


LEGACY = "EMAIL_ORDER_ENTRY_REQUEST"
CANONICAL = "MANUAL_ORDER_INTAKE"


class TestRoutableEventTypeAlias:
    def test_canonical_event_type_in_routable_set(self):
        assert CANONICAL in ROUTABLE_EVENT_TYPES

    def test_legacy_event_type_in_routable_set_during_transition(self):
        # §6.2 transitional dual-acceptance — deadline 2026-08-12.
        assert LEGACY in ROUTABLE_EVENT_TYPES


class TestManualEventTypeAlias:
    def test_canonical_event_type_in_manual_set(self):
        assert CANONICAL in _MANUAL_EVENT_TYPES

    def test_legacy_event_type_in_manual_set_during_transition(self):
        # §6.2 transitional dual-acceptance — deadline 2026-08-12.
        assert LEGACY in _MANUAL_EVENT_TYPES

    def test_short_form_legacy_alias_preserved(self):
        # `EMAIL_ORDER` was a pre-rename shorthand that some producer
        # adapters emit. Stays accepted during the transition.
        assert "EMAIL_ORDER" in _MANUAL_EVENT_TYPES


class TestFallbackBackendIntentClassification:
    """Both names classify to the MANUAL_ORDER_INTAKE intent under
    the fallback backend's event_type → intent map. The active
    backend (LLM / Outlines) is not exercised here — the fallback
    is the deterministic floor that must agree, and a divergence
    between active and fallback is itself a CLAUDE.md §3 violation."""

    @pytest.mark.parametrize("event_type", [CANONICAL, LEGACY, "EMAIL_ORDER"])
    def test_event_type_maps_to_manual_order_intake_intent(self, event_type):
        from constraints.fallback_backend import DeterministicFallbackBackend

        # The fallback inspects event.event_type, event.line_count,
        # and (in non-matching branches) returns None. We supply a
        # minimal duck-typed event so the classifier reaches our
        # branch without dragging in the full GraphState contract.
        class _Event:
            line_count = 0

            def __init__(self, et):
                self.event_type = et

        class _State:
            def __init__(self, et):
                self.event = _Event(et)
                self.intent = None

        decision = DeterministicFallbackBackend().classify_intent(_State(event_type))
        assert decision is not None, (
            f"fallback backend returned None for event_type={event_type!r}; "
            "expected MANUAL_ORDER_INTAKE intent decision"
        )
        assert decision.intent == "MANUAL_ORDER_INTAKE"


class TestAutonomyLevelsAlias:
    def test_canonical_constant_present(self):
        from contracts.policy import MANUAL_ORDER_INTAKE_AUTONOMY_LEVELS
        # The canonical constant must remain the dict the recipe
        # consumes; aliasing direction (legacy → canonical) is the
        # design.
        assert isinstance(MANUAL_ORDER_INTAKE_AUTONOMY_LEVELS, dict)
        assert "ONE_CLICK_APPROVE" in MANUAL_ORDER_INTAKE_AUTONOMY_LEVELS

    def test_legacy_constant_is_same_object(self):
        # Identity test — the legacy alias must be `is` the
        # canonical mapping, not a copy. A copy would silently
        # drift if either side mutated.
        from contracts.policy import (
            EMAIL_ORDER_ENTRY_AUTONOMY_LEVELS,
            MANUAL_ORDER_INTAKE_AUTONOMY_LEVELS,
        )
        assert EMAIL_ORDER_ENTRY_AUTONOMY_LEVELS is MANUAL_ORDER_INTAKE_AUTONOMY_LEVELS


class TestRecipeNameAlias:
    """`recipes/registry.py` carries both keys; both resolve to
    the same RecipeSpec for the §6.2 transitional window."""

    def test_canonical_recipe_name_resolves(self):
        from recipes.registry import get_recipe
        spec = get_recipe("ManualOrderIntakeRecipe.py")
        assert spec.name == "ManualOrderIntakeRecipe.py"

    def test_legacy_recipe_name_resolves(self):
        from recipes.registry import get_recipe
        spec = get_recipe("EmailOrderEntryRecipe.py")
        # Identity preserved — audit-log rows that recorded the
        # legacy name round-trip byte-equal.
        assert spec.name == "EmailOrderEntryRecipe.py"

    def test_both_specs_share_the_same_func(self):
        # If they diverged, a re-fetched legacy row would route to
        # a different classifier — a SOX-relevant drift.
        from recipes.registry import get_recipe
        assert get_recipe("EmailOrderEntryRecipe.py").func is (
            get_recipe("ManualOrderIntakeRecipe.py").func
        )

    def test_module_re_export_alias(self):
        # Both function names resolve to the same callable, so
        # imports under either name route to the same logic.
        from recipes.EmailOrderEntryRecipe import (
            classify_email_order_entry,
        )
        from recipes.ManualOrderIntakeRecipe import (
            classify_manual_order_intake,
        )
        assert classify_email_order_entry is classify_manual_order_intake
