"""'Similar past cases' precedents composer (sign-off 2026-06-10).

Assembles the `PrecedentsAnalysis` payload the UI's Layer-2 evidence
card projects. Two match paths, recorded per-row in `match_basis`:

  * ``semantic``  — cosine similarity over embeddings of a
    deterministic "precedent document" rendered from already-decided
    record fields. Available only when an embedding provider is
    configured (`llm.embeddings.get_embedder`).
  * ``correlate`` — deterministic fallback (no provider / no network):
    same intent + same account first, then same intent, recency-ranked.
    Correlate rows carry NO similarity score — fabricating one would be
    partial-truth.

Guardrail boundary: precedents are advisory retrieval for the human
operator ONLY. No recipe, shadow, or routing decision reads them, so
compliance routing is untouched and constrained generation does not
apply (nothing here is machine-consumed LLM output).

Determinism: ranking uses a stable composite key (similarity desc /
recency desc, then resolved_at desc, then id) so the same store state
always yields the same precedent list.

Embedding cache: vectors are memoised on the record object
(`context_embedding` — the attribute the V001 pgvector column
anticipates) so repeat analysis calls don't re-embed unchanged
candidates. DB-persisted vectors + the HNSW index migration are the
flagged follow-up; this phase serves the in-memory store that backs
the runtime today.

Pure functions over supplied candidates — tenant scoping and terminal
filtering are the ROUTE's job; this module never touches a store.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, List, Optional, Sequence

from api.schemas import PrecedentCase, PrecedentsAnalysis

logger = logging.getLogger(__name__)

# Top-K precedents on the card (sign-off: max 3).
PRECEDENT_LIMIT = 3

# Cap on how many candidates get embedded lazily in one request, newest
# first — bounds provider cost until vectors persist in the DB column.
EMBED_CANDIDATE_CAP = 50


def render_precedent_query(record: Any) -> Optional[str]:
    """Deterministic 'precedent document' for a record.

    A pipe-joined projection of already-decided fields — no free-form
    generation, so the same record always renders the same document
    (persisted as `query_basis` for audit reconstruction). None when
    the record carries nothing matchable (bare record → no card).
    """
    parts: List[str] = []
    intent = getattr(record, "intent", None)
    if intent:
        parts.append(f"intent={intent}")
    account = getattr(record, "account_name", None) or getattr(
        record, "account_id", None
    )
    if account:
        parts.append(f"customer={account}")
    event_type = getattr(record, "event_type", None)
    if event_type:
        parts.append(f"event={event_type}")
    # The governed per-intent one-liner — same vocabulary the queue and
    # the Situation headline use (single source, no drift).
    try:
        from api.case_summary_templates import render_template

        one_liner = render_template(record, None).one_liner
        if one_liner:
            parts.append(f"situation={one_liner}")
    except Exception:  # template gap — the other parts still match
        pass
    notes = getattr(record, "resolution_notes", None)
    if notes:
        parts.append(f"resolution={notes}")
    if not parts:
        return None
    return " | ".join(parts)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _resolved_at(record: Any) -> str:
    return getattr(record, "updated_at", None) or ""


def _to_precedent(
    record: Any,
    *,
    match_basis: str,
    similarity: Optional[float] = None,
    embedding_model: Optional[str] = None,
) -> PrecedentCase:
    """Pure projection of an already-resolved record into a row."""
    outcome = getattr(record, "resolved_action", None) or getattr(
        record, "final_status", None
    )
    return PrecedentCase(
        record_id=record.id,
        case_id=getattr(record, "parent_case_id", None),
        customer_name=getattr(record, "account_name", None),
        intent=getattr(record, "intent", None),
        resolved_at=_resolved_at(record) or None,
        outcome=outcome,
        outcome_summary=getattr(record, "resolution_notes", None),
        similarity=similarity,
        match_basis=match_basis,  # type: ignore[arg-type]
        embedding_model=embedding_model,
    )


def _embedding_of(record: Any, embedder: Any) -> Optional[List[float]]:
    """Vector for a record, memoised on `context_embedding` (the V001
    column's attribute). None when the record renders no document."""
    cached = getattr(record, "context_embedding", None)
    if cached:
        return cached
    doc = render_precedent_query(record)
    if not doc:
        return None
    vector = embedder.embed([doc])[0]
    try:
        record.context_embedding = vector
    except Exception:  # frozen/foreign record shape — skip the cache
        pass
    return vector


def compose_precedents(
    record: Any,
    candidates: Sequence[Any],
    embedder: Any = None,
    *,
    limit: int = PRECEDENT_LIMIT,
) -> Optional[PrecedentsAnalysis]:
    """Assemble the precedents payload for `record`.

    `candidates` must already be tenant-scoped, terminal-state records
    (route's responsibility); self is excluded here defensively. Returns
    None when there is nothing honest to show (no query document or no
    matching precedent) — the UI structurally omits the card.
    """
    query_doc = render_precedent_query(record)
    if not query_doc:
        return None

    pool = [c for c in candidates if c.id != record.id]
    if not pool:
        return None

    items: List[PrecedentCase] = []

    if embedder is not None:
        items = _semantic_match(record, query_doc, pool, embedder, limit)

    if not items:
        items = _correlate_match(record, pool, limit)

    if not items:
        return None

    return PrecedentsAnalysis(
        items=items,
        query_basis=query_doc,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def _semantic_match(
    record: Any,
    query_doc: str,
    pool: Sequence[Any],
    embedder: Any,
    limit: int,
) -> List[PrecedentCase]:
    """Cosine ranking over lazily-embedded candidates. Any provider
    failure degrades to [] (caller falls back to correlate) — the
    analysis endpoint must never 500 over an advisory card."""
    try:
        query_vec = embedder.embed([query_doc])[0]
        # Newest candidates first under the embed cap — recent
        # precedents are worth the embedding spend, ancient ones wait
        # for the DB backfill.
        ranked_pool = sorted(pool, key=_resolved_at, reverse=True)[
            :EMBED_CANDIDATE_CAP
        ]
        scored: List[tuple[float, str, str, Any]] = []
        for cand in ranked_pool:
            vec = _embedding_of(cand, embedder)
            if vec is None:
                continue
            sim = _cosine(query_vec, vec)
            scored.append((sim, _resolved_at(cand), cand.id, cand))
        # One deterministic sort: similarity desc, then resolved_at
        # desc, then id asc (tie-break of last resort).
        scored.sort(key=lambda t: (-round(t[0], 6), _inverted(t[1]), t[2]))
        return [
            _to_precedent(
                cand,
                match_basis="semantic",
                similarity=round(sim, 4),
                embedding_model=getattr(embedder, "model", None),
            )
            for sim, _ts, _id, cand in scored[:limit]
        ]
    except Exception as exc:
        logger.warning("Semantic precedent matching unavailable: %s", exc)
        return []


def _inverted(ts: str) -> float:
    """Numeric key that inverts an ISO timestamp inside an ascending
    composite sort (newer → smaller key → first). Unparseable input
    sorts after every real timestamp."""
    try:
        return -datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return float("inf")


def _correlate_match(
    record: Any, pool: Sequence[Any], limit: int
) -> List[PrecedentCase]:
    """Deterministic fallback: same intent + same account first, then
    same intent, recency-ranked. No similarity score is fabricated."""
    intent = getattr(record, "intent", None)
    if not intent:
        return []
    account = getattr(record, "account_id", None)
    same_intent = [c for c in pool if getattr(c, "intent", None) == intent]
    tier_one = [
        c
        for c in same_intent
        if account and getattr(c, "account_id", None) == account
    ]
    tier_two = [c for c in same_intent if c not in tier_one]
    by_recency = lambda c: (_inverted(_resolved_at(c)), c.id)  # noqa: E731
    ordered = sorted(tier_one, key=by_recency) + sorted(tier_two, key=by_recency)
    return [
        _to_precedent(c, match_basis="correlate") for c in ordered[:limit]
    ]
