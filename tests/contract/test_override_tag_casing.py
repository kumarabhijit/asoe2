"""Spec-as-oracle: AllowedOverrideReasonTag casing convention.

ADR-033 V1 §1 documents the convention:
  - Legacy / global codes are lowercase.
  - Per-intent curated codes are SCREAMING_SNAKE_CASE.
  - Both coexist in the global Literal; the case identifies the
    vocabulary an entry belongs to.

This test enforces:
  1. Every value in `AllowedOverrideReasonTag` is either fully lowercase
     or fully SCREAMING_SNAKE_CASE — no MixedCase, no kebab-case.
  2. Legacy (lowercase) tuple `_GLOBAL_REASON_TAGS` is fully lowercase.
  3. Curated per-intent tuples are fully SCREAMING_SNAKE_CASE.
  4. There is no value present in BOTH the lowercase and uppercase
     spellings (e.g. "other" + "OTHER" both legal but a duplicate
     across vocabularies is a bug, not a coincidence).

Reference: docs/test-strategy/eng-review-test-plan.md (Regression tests
required, item #3 — `tests/contract/test_override_tag_casing.py`).
"""

from __future__ import annotations

import re
from typing import get_args

import pytest

from constraints.specs import (
    AllowedOverrideReasonTag,
    INTENT_REASON_TAGS,
    _DUPLICATE_PO_REASON_TAGS,
    _GLOBAL_REASON_TAGS,
)


_LOWER = re.compile(r"^[a-z][a-z0-9_]*$")
_SCREAMING = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _classify(tag: str) -> str:
    if _LOWER.fullmatch(tag):
        return "lower"
    if _SCREAMING.fullmatch(tag):
        return "SCREAMING"
    return "MIXED"


_TAGS = list(get_args(AllowedOverrideReasonTag))


@pytest.mark.parametrize("tag", _TAGS, ids=lambda s: s)
def test_every_tag_is_lower_or_screaming(tag: str) -> None:
    """ADR-033 forbids MixedCase, kebab-case, or any other style."""
    style = _classify(tag)
    assert style != "MIXED", (
        f"AllowedOverrideReasonTag value {tag!r} is neither fully "
        f"lowercase (legacy) nor fully SCREAMING_SNAKE_CASE (curated). "
        f"Per ADR-033 §C.2 every tag must be one or the other."
    )


def test_global_tuple_is_all_lowercase() -> None:
    """Legacy tuple may only contain lowercase entries."""
    bad = [t for t in _GLOBAL_REASON_TAGS if _classify(t) != "lower"]
    assert not bad, (
        f"_GLOBAL_REASON_TAGS must be all lowercase; offenders: {bad}"
    )


def test_duplicate_po_tuple_is_all_screaming() -> None:
    """The first curated per-intent vocabulary must be uppercase."""
    bad = [t for t in _DUPLICATE_PO_REASON_TAGS if _classify(t) != "SCREAMING"]
    assert not bad, (
        f"_DUPLICATE_PO_REASON_TAGS must be SCREAMING_SNAKE_CASE; "
        f"offenders: {bad}"
    )


def test_no_case_collision_across_vocabularies() -> None:
    """If a value appears in both a lowercase and an uppercase
    vocabulary it's a near-miss bug — `OTHER` is the explicit
    exception (it lives in both because the workflow-safety fallback
    must be present in every per-intent set, and ADR-033 §C.2
    permits both spellings)."""
    lowered = {t for t in _TAGS if _classify(t) == "lower"}
    uppered = {t.lower() for t in _TAGS if _classify(t) == "SCREAMING"}
    collision = lowered & uppered
    permitted = {"other"}
    unexpected = collision - permitted
    assert not unexpected, (
        f"Same logical reason has both casings: {sorted(unexpected)}. "
        f"This is a vocabulary fork — pick one and dedupe."
    )


def test_every_per_intent_set_ends_with_other() -> None:
    """ADR-033 §C.2 — the workflow-safety fallback is mandatory and
    must be the last element so the /disposition UI's chooser can
    always present an `Other → free-text` row at the bottom."""
    for intent, tags in INTENT_REASON_TAGS.items():
        last = tags[-1]
        assert last in ("OTHER", "other"), (
            f"Per-intent vocabulary for {intent!r} must end with "
            f"'OTHER' or 'other'; got {last!r}. Sequence: {tags}"
        )


def test_every_per_intent_value_is_in_global_literal() -> None:
    """The /disposition endpoint validates against the global
    Literal first; an entry in a per-intent tuple that isn't in the
    global Literal causes an unreachable disposition path."""
    global_set = set(_TAGS)
    for intent, tags in INTENT_REASON_TAGS.items():
        missing = set(tags) - global_set
        assert not missing, (
            f"Per-intent vocabulary for {intent!r} contains values "
            f"missing from AllowedOverrideReasonTag: {sorted(missing)}."
        )
