"""Spatial evidence extraction (ADR-045) — document-AI as a CANDIDATE PROPOSER.

The load-bearing safety property: the system never free-generates geometry.

  1. An OCR / layout pass over the frozen rendition (ADR-044) yields a CLOSED
     candidate set of ``{candidate_id, page, bbox, text}``.
  2. A field is associated to a candidate by SELECTION over that set
     (`select_candidate_box`) — an out-of-set id raises, so a hallucinated
     coordinate is structurally impossible.
  3. Every spatial anchor passes the runtime verifier (`verify_anchor_geometry`):
     the rendered text under the chosen box must match the anchor text, else the
     anchor DEGRADES to an ADR-043 text anchor. Geometry is
     ``required_for_audit = False`` — an OCR outage or a mismatch never blocks
     the operator view, it just falls back to text-level highlighting.

A real OCR / layout provider (managed OCR or a self-hosted layout model)
implements the candidate proposer behind the gateway seam; per the ratified test
strategy, red-green never hits a live model — coordinate outputs are replayed
from recorded fixtures and scored by the eval harness (`tests/eval/`).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from api.schemas import EvidenceAnchor, MatchKey


def _normalize(s: Any) -> str:
    """Same normalisation as the text-anchor locate key (collapse whitespace +
    casefold) so the verifier compares like-for-like."""
    return " ".join(str(s).split()).casefold()


def select_candidate_box(candidates: Sequence[Dict[str, Any]], chosen_id: int) -> Dict[str, Any]:
    """Return the OCR candidate whose ``candidate_id`` equals ``chosen_id``.

    Raises ``ValueError`` if the id is not in the candidate set — the model may
    only SELECT a real box the OCR produced, never invent one (ADR-045 §2.1).
    """
    for c in candidates:
        if c.get("candidate_id") == chosen_id:
            return c
    raise ValueError(f"chosen_id {chosen_id!r} is not in the candidate set")


def verify_anchor_geometry(
    anchor: EvidenceAnchor,
    rendered_text_under_box: Optional[str] = None,
) -> EvidenceAnchor:
    """Runtime verifier (ADR-045 §2.2).

    Keep a spatial anchor's geometry only when the rendered text under its box
    matches the anchor text; otherwise DEGRADE to a text-derived anchor (drop
    ``page``/``bbox``/``confidence``). The viewer never draws a confident box
    over unverified content. Text-derived anchors pass through unchanged.
    """
    if anchor.anchor_source != "spatial_extracted":
        return anchor
    needle = _normalize(anchor.text)
    haystack = _normalize(rendered_text_under_box) if rendered_text_under_box is not None else ""
    if needle and needle in haystack:
        return anchor
    return anchor.model_copy(
        update={
            "anchor_source": "text_derived",
            "page": None,
            "bbox": None,
            "confidence": None,
        }
    )


def build_spatial_anchor(
    *,
    candidates: Sequence[Dict[str, Any]],
    chosen_id: int,
    rendered_text_under_box: Optional[str],
    attachment_id: str,
    text: str,
    match_key: MatchKey,
    supports_kind: str,
    supports_ref: str,
    label: str,
    source_sha256: str,
    confidence: Optional[float] = None,
) -> EvidenceAnchor:
    """Build a VERIFIED spatial anchor: SELECT a candidate box (never generate
    one), construct the anchor, then run the verifier. Returns a spatial anchor
    when verified, else a degraded text anchor (geometry is not required for
    audit). This is the single place a spatial anchor is minted.
    """
    box = select_candidate_box(candidates, chosen_id)
    anchor = EvidenceAnchor(
        attachment_id=attachment_id,
        anchor_source="spatial_extracted",
        text=text,
        match_key=match_key,
        supports_kind=supports_kind,  # type: ignore[arg-type]
        supports_ref=supports_ref,
        label=label,
        source_sha256=source_sha256,
        page=int(box["page"]),
        bbox=[float(x) for x in box["bbox"]],
        confidence=confidence,
    )
    return verify_anchor_geometry(anchor, rendered_text_under_box)


__all__: List[str] = [
    "select_candidate_box",
    "verify_anchor_geometry",
    "build_spatial_anchor",
]
