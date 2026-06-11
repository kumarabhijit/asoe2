"""'Similar past cases' precedents composer locks (sign-off 2026-06-10).

Deterministic — no LLM / network. The semantic path is exercised with a
fake embedder producing fixed vectors; the correlate path needs no
provider at all (it IS the no-provider behaviour).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import List, Sequence

import pytest

from api.precedents_composer import (
    PRECEDENT_LIMIT,
    compose_precedents,
    render_precedent_query,
)


def _record(
    id: str,
    *,
    intent: str | None = "DUPLICATE_PO",
    account_id: str | None = "ACME",
    account_name: str | None = "Acme Beverages",
    updated_at: str = "2026-06-01T00:00:00+00:00",
    resolved_action: str | None = None,
    final_status: str | None = "COMPLETE",
    resolution_notes: str | None = None,
    parent_case_id: str | None = None,
    context_embedding: List[float] | None = None,
):
    return SimpleNamespace(
        id=id,
        intent=intent,
        account_id=account_id,
        account_name=account_name,
        event_type="EDI_850_PRICE_MISMATCH",
        updated_at=updated_at,
        resolved_action=resolved_action,
        final_status=final_status,
        resolution_notes=resolution_notes,
        parent_case_id=parent_case_id,
        enrichment_context=None,
        context_embedding=context_embedding,
    )


class FakeEmbedder:
    """Deterministic embedder: maps each text to a fixed vector via a
    lookup, so cosine ranking is fully scripted."""

    model = "fake-embedder-v1"

    def __init__(self, vector_for: dict[str, List[float]], default=None):
        self.vector_for = vector_for
        self.default = default or [0.0, 0.0, 1.0]
        self.calls: List[Sequence[str]] = []

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        self.calls.append(texts)
        return [self.vector_for.get(t, self.default) for t in texts]


# ── query document ─────────────────────────────────────────────────────


def test_query_document_is_deterministic_projection():
    rec = _record("q", resolution_notes="released after dedup check")
    doc = render_precedent_query(rec)
    assert doc is not None
    assert "intent=DUPLICATE_PO" in doc
    assert "customer=Acme Beverages" in doc
    assert "resolution=released after dedup check" in doc
    # Same record → same document (audit reconstruction).
    assert render_precedent_query(rec) == doc


def test_bare_record_renders_no_document_and_no_card():
    bare = SimpleNamespace(
        id="bare",
        intent=None,
        account_id=None,
        account_name=None,
        event_type=None,
        resolution_notes=None,
        enrichment_context=None,
    )
    assert render_precedent_query(bare) is None
    assert compose_precedents(bare, [_record("c1")]) is None


# ── correlate fallback (no provider) ───────────────────────────────────


def test_correlate_prefers_same_customer_then_recency():
    rec = _record("query")
    other_customer = _record(
        "p-other",
        account_id="KRG",
        account_name="Kroger",
        updated_at="2026-06-09T00:00:00+00:00",
    )
    same_old = _record("p-old", updated_at="2026-03-01T00:00:00+00:00")
    same_new = _record("p-new", updated_at="2026-05-01T00:00:00+00:00")
    result = compose_precedents(rec, [other_customer, same_old, same_new])
    assert result is not None
    ids = [i.record_id for i in result.items]
    # Same-customer first (newest→oldest), then other customers.
    assert ids == ["p-new", "p-old", "p-other"]
    assert all(i.match_basis == "correlate" for i in result.items)
    # No fabricated similarity on correlate rows.
    assert all(i.similarity is None for i in result.items)
    assert all(i.embedding_model is None for i in result.items)


def test_correlate_excludes_other_intents_and_self():
    rec = _record("query")
    wrong_intent = _record("p-wrong", intent="CREDIT_BLOCK")
    result = compose_precedents(rec, [wrong_intent, rec])
    assert result is None  # nothing honest to show


def test_limit_is_capped():
    rec = _record("query")
    pool = [
        _record(f"p{i}", updated_at=f"2026-05-{i + 1:02d}T00:00:00+00:00")
        for i in range(6)
    ]
    result = compose_precedents(rec, pool)
    assert result is not None
    assert len(result.items) == PRECEDENT_LIMIT


# ── semantic path (fake embedder) ──────────────────────────────────────


def _semantic_fixture():
    rec = _record("query", resolution_notes=None)
    near = _record("p-near", account_id="KRG", account_name="Kroger")
    mid = _record("p-mid", account_id="SYS", account_name="Sysco")
    far = _record("p-far", account_id="USF", account_name="US Foods")
    qdoc = render_precedent_query(rec)
    embedder = FakeEmbedder(
        {
            qdoc: [1.0, 0.0, 0.0],
            render_precedent_query(near): [0.99, 0.1, 0.0],
            render_precedent_query(mid): [0.5, 0.5, 0.0],
            render_precedent_query(far): [0.0, 1.0, 0.0],
        }
    )
    return rec, [far, mid, near], embedder


def test_semantic_ranks_by_cosine_and_records_provenance():
    rec, pool, embedder = _semantic_fixture()
    result = compose_precedents(rec, pool, embedder)
    assert result is not None
    ids = [i.record_id for i in result.items]
    assert ids == ["p-near", "p-mid", "p-far"]
    top = result.items[0]
    assert top.match_basis == "semantic"
    assert top.similarity is not None and top.similarity > 0.9
    assert top.embedding_model == "fake-embedder-v1"
    # The exact matched document is preserved for audit reconstruction.
    assert result.query_basis == render_precedent_query(rec)


def test_semantic_is_deterministic_across_calls():
    rec, pool, embedder = _semantic_fixture()
    a = compose_precedents(rec, pool, embedder)
    b = compose_precedents(rec, pool, embedder)
    assert [i.record_id for i in a.items] == [i.record_id for i in b.items]
    assert [i.similarity for i in a.items] == [i.similarity for i in b.items]


def test_semantic_caches_candidate_vectors_on_the_record():
    rec, pool, embedder = _semantic_fixture()
    compose_precedents(rec, pool, embedder)
    first_call_count = len(embedder.calls)
    compose_precedents(rec, pool, embedder)
    # Second pass re-embeds only the query (candidates memoised on
    # context_embedding — the V001 column's attribute).
    assert len(embedder.calls) == first_call_count + 1
    assert all(c.context_embedding is not None for c in pool)


def test_semantic_provider_failure_degrades_to_correlate():
    class ExplodingEmbedder:
        model = "boom"

        def embed(self, texts):
            raise RuntimeError("provider down")

    rec = _record("query")
    pool = [_record("p1")]
    result = compose_precedents(rec, pool, ExplodingEmbedder())
    assert result is not None
    assert result.items[0].match_basis == "correlate"


def test_outcome_prefers_human_action_over_terminal_status():
    rec = _record("query")
    human = _record("p-h", resolved_action="OVERRIDE_APPROVE")
    auto = _record("p-a", updated_at="2026-01-01T00:00:00+00:00")
    result = compose_precedents(rec, [human, auto])
    by_id = {i.record_id: i for i in result.items}
    assert by_id["p-h"].outcome == "OVERRIDE_APPROVE"
    assert by_id["p-a"].outcome == "COMPLETE"
