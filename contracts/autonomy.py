"""Versioned autonomy vocabulary (ADR-042 §5).

Two vocabularies coexist so historical records keep their original meaning
while new records use the product-chosen ordering:

  * **v1** (legacy / `contracts/policy.py`): L1 = observe only … L4 = full
    autonomy. Rank ascends with automation.
  * **v2** (product decision, 2026-05-23 — the AgenticOM prototype ordering):
    L1 = most autonomous (auto-reply) … L4 = human escalation. Rank descends.

`autonomy_rank` returns the *degree of automation* (higher == more autonomous),
so the two ladders are mirror images. A record stamped with its
`autonomy_vocab_version` always resolves under that version — the same "L1"
string means opposite things across versions, which is exactly the
audit-integrity property the gate test locks (no silent reinterpretation).

`CURRENT_AUTONOMY_VOCAB_VERSION` is what newly-created records stamp.
"""

from __future__ import annotations

from typing import Dict

CURRENT_AUTONOMY_VOCAB_VERSION = "v2"

# degree-of-automation rank: higher == more autonomous.
_RANKS: Dict[str, Dict[str, int]] = {
    "v1": {"L1": 0, "L2": 1, "L3": 2, "L4": 3},  # observe → full autonomy
    "v2": {"L1": 3, "L2": 2, "L3": 1, "L4": 0},  # auto-reply → human
}

_LABELS: Dict[str, Dict[str, str]] = {
    "v1": {
        "L1": "Observe only",
        "L2": "Recommend",
        "L3": "Act & inform",
        "L4": "Full autonomy",
    },
    "v2": {
        "L1": "Auto — no human review",
        "L2": "Execute & notify",
        "L3": "Prepare & await approval",
        "L4": "Escalate to human",
    },
}


def autonomy_rank(level: str, vocab_version: str) -> int:
    """Degree of automation for ``level`` under ``vocab_version`` (higher ==
    more autonomous). Fails closed on an unknown level/version."""
    try:
        return _RANKS[vocab_version][level]
    except KeyError as exc:
        raise ValueError(
            f"unknown autonomy level/version: {level!r}/{vocab_version!r}"
        ) from exc


def autonomy_label(level: str, vocab_version: str) -> str:
    """Operator-facing label for ``level`` under ``vocab_version``."""
    try:
        return _LABELS[vocab_version][level]
    except KeyError as exc:
        raise ValueError(
            f"unknown autonomy level/version: {level!r}/{vocab_version!r}"
        ) from exc


def allowed_autonomy_levels(
    vocab_version: str = CURRENT_AUTONOMY_VOCAB_VERSION,
) -> list[dict]:
    """The operator-facing autonomy vocabulary for ``vocab_version`` as
    ``{level, label, rank}`` rows (rank == degree of automation).

    Surfaced on ``/health`` so the UI sorts/labels by ``rank`` at runtime
    instead of hardcoding a map (asoe-ui Guardrail #1). Display-only — this
    does NOT change the numeric→behaviour gating recipes dispatch on (that
    coherent v2 migration is separately gated). Fails closed on an unknown
    version.
    """
    try:
        ranks = _RANKS[vocab_version]
        labels = _LABELS[vocab_version]
    except KeyError as exc:
        raise ValueError(f"unknown autonomy vocab version: {vocab_version!r}") from exc
    return [
        {"level": level, "label": labels[level], "rank": ranks[level]}
        for level in ("L1", "L2", "L3", "L4")
    ]
