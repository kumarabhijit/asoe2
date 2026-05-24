"""Phase-0 RED gate (backlog #4) — /health exposes the autonomy vocabulary.

The UI must source autonomy ordering + labels at runtime from
`/api/v1/health.allowed_autonomy_levels` (rank-sorted), never from hardcoded
maps (asoe-ui Guardrail #1). The field carries the v2 (prototype-ordering)
vocabulary under `autonomy_vocab_version` (ADR-042 §5).

xfail(strict): green while unimplemented; flips to a hard CI failure once the
field ships — which coincides with the gated autonomy-vocab v2 flip
(human + compliance dual-control sign-off). Health takes no auth (mirrors
tests/test_v1_guardrails.py::test_health_serves_all_allowed_intents).
"""

from __future__ import annotations

import pytest


@pytest.mark.xfail(
    reason="ADR-042 §5/§8: health.allowed_autonomy_levels not implemented "
    "(gated on autonomy-vocab v2 sign-off)",
    strict=True,
)
def test_health_exposes_ranked_autonomy_levels(client) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert "allowed_autonomy_levels" in body
    levels = body["allowed_autonomy_levels"]
    assert isinstance(levels, list) and levels
    for entry in levels:
        assert {"level", "label", "rank"} <= set(entry)
    # Ranks must be a total order so the UI can sort deterministically.
    ranks = [e["rank"] for e in levels]
    assert len(set(ranks)) == len(ranks)
