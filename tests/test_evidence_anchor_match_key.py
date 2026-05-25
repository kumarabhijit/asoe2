"""CP-B RED gate (ADR-043 §2.4) — deterministic MatchKey.

Test-first (`xfail(strict=True)`; removed at CP-C). The backend supplies a
deterministic locate key so the UI can do a pure literal locate (no client-side
search → Guardrail #6). Repeated tokens (the AMBIGUOUS hazard) must get distinct
occurrence indices, and the key must be stable across calls.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.xfail(
    reason="ADR-043 compute_match_keys lands at CP-C",
    strict=True,
)


def test_repeated_value_gets_distinct_occurrence_indices():
    from api.analysis_adapters import compute_match_keys

    keys = compute_match_keys(["PO 12345", "po  12345", "Acme Corp"])
    # First two normalise to the same text → occurrences 0 and 1.
    assert keys[0].normalized_text == keys[1].normalized_text
    assert keys[0].occurrence_index == 0
    assert keys[1].occurrence_index == 1
    # A distinct value starts its own occurrence count.
    assert keys[2].occurrence_index == 0


def test_normalisation_collapses_whitespace_and_case():
    from api.analysis_adapters import compute_match_keys

    (k,) = compute_match_keys(["  PO\t 12345 \n"])
    assert k.normalized_text == "po 12345"


def test_is_deterministic():
    from api.analysis_adapters import compute_match_keys

    values = ["A", "a", "B", "A"]
    assert compute_match_keys(values) == compute_match_keys(values)
