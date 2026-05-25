"""CP-B RED gate (ADR-045 §2.1, §2.2) — select-not-generate + the runtime verifier.

Test-first (`xfail(strict=True)`; removed at CP-F). The load-bearing Phase-2
safety property: the model never free-generates geometry, and a spatial anchor is
only kept if the rendered text under its box equals the anchor text. A mismatch
(or an extraction outage) degrades to an ADR-043 text anchor — never a confident
box over unverified content.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.xfail(
    reason="ADR-045 document_extraction verifier lands at CP-F",
    strict=True,
)


def _spatial_anchor(**overrides):
    from api.schemas import EvidenceAnchor, MatchKey

    base = dict(
        attachment_id="att-1",
        anchor_source="spatial_extracted",
        text="PO-2026-0042",
        match_key=MatchKey(normalized_text="po-2026-0042", occurrence_index=0),
        supports_kind="extracted_field",
        supports_ref="order_entry.po_number",
        label="PO number",
        source_sha256="a" * 64,
        page=1,
        bbox=[0.1, 0.1, 0.3, 0.15],
        confidence=0.92,
    )
    base.update(overrides)
    return EvidenceAnchor(**base)


def test_verifier_keeps_geometry_when_box_text_matches():
    from gateways.document_extraction import verify_anchor_geometry

    a = verify_anchor_geometry(_spatial_anchor(), rendered_text_under_box="PO-2026-0042")
    assert a.anchor_source == "spatial_extracted"
    assert a.page == 1 and a.bbox is not None


def test_verifier_drops_geometry_on_text_mismatch():
    # Box text != anchor text → confidently-wrong box. Degrade to a text anchor.
    from gateways.document_extraction import verify_anchor_geometry

    a = verify_anchor_geometry(_spatial_anchor(), rendered_text_under_box="SOMETHING ELSE")
    assert a.anchor_source == "text_derived"
    assert a.page is None and a.bbox is None and a.confidence is None


def test_geometry_is_selected_from_candidates_never_generated():
    # The selector may only choose a candidate box the OCR actually produced;
    # an out-of-set index is rejected (hallucinated coordinates impossible).
    from gateways.document_extraction import select_candidate_box

    candidates = [
        {"candidate_id": 0, "page": 1, "bbox": [0.1, 0.1, 0.3, 0.15], "text": "PO-2026-0042"},
    ]
    with pytest.raises(ValueError):
        select_candidate_box(candidates, chosen_id=99)
