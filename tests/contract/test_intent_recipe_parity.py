"""Spec-as-oracle: intent ↔ recipe parity.

Reads `constraints/specs.py::AllowedIntent` and asserts:

  1. Every intent that has a recipe path has a registered recipe in
     `recipes.registry.REGISTRY`.
  2. Every recipe in `AllowedRecipeName` is registered in the recipe
     registry (already covered by `tests/test_registry.py` but
     duplicated here as a contract-layer assertion).
  3. Every intent has either a recipe path (via INTENT_TO_RECIPE_NAME)
     or is documented as a no-recipe intent (RED-only / no-skill).
  4. Every value in `AllowedIntent` appears in `INTENT_REASON_TAGS`
     (override-reason vocabulary completeness — required for the
     `/disposition` endpoint).

Reference: docs/test-strategy/eng-review-test-plan.md (Critical Paths,
Key Interactions §1).
"""

from __future__ import annotations

from typing import get_args

import pytest

from api.analysis_adapters import INTENT_TO_RECIPE_NAME
from constraints.specs import (
    AllowedIntent,
    AllowedRecipeName,
    INTENT_REASON_TAGS,
)
from recipes.registry import REGISTRY


# Intents that are intentionally not bound to a recipe. These route
# either to a RED shadow verdict (BLOCKED) or to FAIL_TO_HUMAN before
# select_recipe runs. Adding a new entry here requires a paired ADR
# update or a recipe — not silent expansion.
NO_RECIPE_INTENTS: frozenset[str] = frozenset({
    "MASS_PRICING_ERROR",  # RED → BLOCKED, no recipe by design
    "CREDIT_BLOCK",        # YELLOW → MANUAL_REVIEW_REQUIRED
                           # CreditHoldReleaseRecipe.py exists for the
                           # explicit /resolve path but the default
                           # graph routes credit-block events to human
                           # review, so INTENT_TO_RECIPE_NAME omits it.
})


_INTENTS = list(get_args(AllowedIntent))


@pytest.mark.parametrize("intent", _INTENTS, ids=lambda s: s)
def test_every_intent_has_recipe_or_is_no_recipe(intent: str) -> None:
    """Every intent in the vocabulary either maps to a registered
    recipe or is on the no-recipe allow-list."""
    if intent in NO_RECIPE_INTENTS:
        return
    recipe_name = INTENT_TO_RECIPE_NAME.get(intent)
    assert recipe_name is not None, (
        f"Intent {intent!r} is in AllowedIntent but has no entry in "
        f"INTENT_TO_RECIPE_NAME and is not on the NO_RECIPE_INTENTS "
        f"allow-list. Either add a recipe mapping or document the "
        f"omission in NO_RECIPE_INTENTS with a one-line rationale."
    )
    assert recipe_name in REGISTRY, (
        f"Intent {intent!r} maps to recipe {recipe_name!r} but that "
        f"recipe is not in recipes.registry.REGISTRY. Did the recipe "
        f"file get renamed without updating the registry?"
    )


@pytest.mark.parametrize(
    "recipe_name", list(get_args(AllowedRecipeName)), ids=lambda s: s,
)
def test_every_allowed_recipe_is_registered(recipe_name: str) -> None:
    """Every recipe value in `AllowedRecipeName` must have a registry
    entry. Adding a new recipe to the Literal without registering it
    means classify_intent → select_recipe will return a name the
    executor cannot resolve."""
    assert recipe_name in REGISTRY, (
        f"Recipe {recipe_name!r} is in AllowedRecipeName but missing "
        f"from recipes.registry.REGISTRY. Register it via "
        f"recipes/registry.py REGISTRY[{recipe_name!r}] = RecipeSpec(...)."
    )


@pytest.mark.parametrize("intent", _INTENTS, ids=lambda s: s)
def test_every_intent_has_override_reason_vocabulary(intent: str) -> None:
    """The /disposition endpoint validates override-reason tags against
    `INTENT_REASON_TAGS[intent]`. Every intent must have a vocabulary
    or the endpoint 500s on dispositions for that intent."""
    assert intent in INTENT_REASON_TAGS, (
        f"Intent {intent!r} has no entry in INTENT_REASON_TAGS. The "
        f"/disposition endpoint will 500 on overrides for this intent."
    )
    tags = INTENT_REASON_TAGS[intent]
    assert tags, (
        f"Intent {intent!r} has an empty override-reason vocabulary."
    )
    # Mandatory workflow-safety fallback per ADR-033 §C.2.
    assert "OTHER" in tags or "other" in tags, (
        f"Intent {intent!r} override-reason vocabulary is missing the "
        f"mandatory 'OTHER'/'other' fallback (ADR-033 §C.2)."
    )


def test_no_recipe_intents_are_in_allowed_intent() -> None:
    """The NO_RECIPE_INTENTS allow-list cannot contain a value that
    isn't a real intent — guards against typos that would cause every
    real intent to fall through to the recipe-required path."""
    unknown = NO_RECIPE_INTENTS - set(_INTENTS)
    assert not unknown, (
        f"NO_RECIPE_INTENTS contains values not in AllowedIntent: "
        f"{sorted(unknown)}. Remove them or correct the spelling."
    )


def test_intent_to_recipe_name_keys_are_real_intents() -> None:
    """No typo'd intents in the routing table."""
    unknown = set(INTENT_TO_RECIPE_NAME) - set(_INTENTS)
    assert not unknown, (
        f"INTENT_TO_RECIPE_NAME contains keys not in AllowedIntent: "
        f"{sorted(unknown)}."
    )
