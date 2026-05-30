"""Derived taxonomy lookups (non-generated).

`contracts/_generated/taxonomy_constants.py` is the generated source
of truth for the case taxonomy (projected from
`db/seeds/case_taxonomy.yaml`). This module derives the convenience
inversions the runtime needs *without hand-authoring any mapping* —
regenerating the constants propagates here automatically, so it cannot
drift from the governed taxonomy.

Used by the case-intake classifier to resolve a case's supergroup from
its child's classified intent (requirements §8.3, classifier_type=RULE).
"""

from __future__ import annotations

from typing import Optional

from contracts._generated.taxonomy_constants import INTENTS_BY_SUPERGROUP

# intent_code -> supergroup_code, inverted from the generated SoT. Each
# intent code belongs to exactly one supergroup, so the inversion is
# total and unambiguous.
_SUPERGROUP_BY_INTENT_CODE: dict[str, str] = {
    code: supergroup
    for supergroup, codes in INTENTS_BY_SUPERGROUP.items()
    for code in codes
}


def intent_code_for(intent: str) -> str:
    """Map a bare wire intent (``DUPLICATE_PO``) to its taxonomy intent
    code (``INT_DUPLICATE_PO``).

    The wire/enum namespace and the taxonomy namespace differ only by
    the ``INT_`` prefix (requirements §Q8: "intents: ``INT_*``"). Already-
    prefixed inputs pass through unchanged.
    """
    return intent if intent.startswith("INT_") else f"INT_{intent}"


def supergroup_for_intent(intent: Optional[str]) -> Optional[str]:
    """Resolve the supergroup for a classified intent from the governed
    taxonomy, or ``None`` when the intent is absent or unmapped.

    This is a deterministic ``RULE`` classification: it consumes the
    already-approved ``INTENTS_BY_SUPERGROUP`` mapping and invents
    nothing. ``None`` (rather than a guessed sentinel) is returned for
    an unmapped intent so the case is never mis-classified — a genuinely
    unmapped intent leaves the supergroup unset until a steward maps it.
    """
    if not intent:
        return None
    return _SUPERGROUP_BY_INTENT_CODE.get(intent_code_for(intent))
