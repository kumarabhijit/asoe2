"""Council 2026-06-07 — presentation contract composer tests.

Locks the deterministic placement projection the UI honors (asoe-ui
Guardrail #0): the `show_intent` discriminator and the audit bundle.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from contracts.models import Intent
from api.presentation_composer import (
    _NON_DISCRIMINATING_INTENTS,
    compose_presentation,
    intent_discriminates,
)


def _record(intent=None, recipe=None):
    return SimpleNamespace(intent=intent, selected_recipe=recipe)


@pytest.mark.parametrize(
    "intent,expected",
    [
        ("CREDIT_BLOCK", True),
        ("DUPLICATE_PO", True),
        ("PRICE_HOLD_RELEASE", True),
        ("EDI_MISMATCH", True),
        ("BACK_ORDER", True),
        ("OVER_MAX", True),
        # Channel/intake restatements — do NOT discriminate.
        ("MANUAL_ORDER_INTAKE", False),
        ("UNKNOWN", False),
        (None, False),
        ("", False),
    ],
)
def test_intent_discriminates(intent, expected):
    assert intent_discriminates(intent) is expected


def test_every_intent_enum_value_is_classified():
    """Each canonical Intent is either discriminating or explicitly
    non-discriminating — no value falls through ambiguously. (A new
    problem intent defaults to discriminating, which is the safe L1
    direction; this test documents the current split.)"""
    for member in Intent:
        discriminates = intent_discriminates(member.value)
        in_nondiscriminating = member.value in _NON_DISCRIMINATING_INTENTS
        # discriminating XOR listed-as-non-discriminating
        assert discriminates != in_nondiscriminating


def test_compose_presentation_problem_intent():
    c = compose_presentation(_record(intent="CREDIT_BLOCK", recipe="CreditHoldReleaseRecipe"))
    assert c.show_intent is True
    assert c.audit.recipe_name == "CreditHoldReleaseRecipe"
    assert c.audit.intent_code == "CREDIT_BLOCK"


def test_compose_presentation_channel_intent_hides_intent_but_keeps_audit():
    c = compose_presentation(
        _record(intent="MANUAL_ORDER_INTAKE", recipe="ManualOrderIntakeRecipe")
    )
    # Q3: the channel-intent is NOT shown in L1 ...
    assert c.show_intent is False
    # ... but the raw enum + recipe are never lost — they're in the audit
    # bundle (Guardrail #7: available, not removed).
    assert c.audit.intent_code == "MANUAL_ORDER_INTAKE"
    assert c.audit.recipe_name == "ManualOrderIntakeRecipe"


def test_compose_presentation_missing_fields():
    c = compose_presentation(_record())
    assert c.show_intent is False
    assert c.audit.recipe_name is None
    assert c.audit.intent_code is None
