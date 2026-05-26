"""ADR-044 §2.5 / P2.4 — frozen renditions bind spatial geometry to a render basis.

A spatial bbox (ADR-045) is meaningless without the exact page render it was
computed against. A frozen rendition pins the raster bytes + dpi +
renderer_version and hashes them; the coordinate-validity invariant is that a
re-render under a *different* basis (dpi / renderer bump) is a hard failure, not
a silently-misplaced box. Minimal, just enough for ADR-045 geometry to be
verifiable. (Erasure-cascade of renditions is governance — out of scope.)
"""

from __future__ import annotations

import pytest

from gateways.attachment_store import _InMemoryBlobStore
from gateways.frozen_rendition import (
    RenderBasisMismatch,
    compute_rendition_hash,
    freeze_rendition,
    verify_render_basis,
)


def test_rendition_hash_is_deterministic_and_basis_sensitive():
    h1 = compute_rendition_hash(
        attachment_sha256="a" * 64, page=1, dpi=150,
        renderer_version="pdfjs-4.2", raster=b"RASTER",
    )
    h2 = compute_rendition_hash(
        attachment_sha256="a" * 64, page=1, dpi=150,
        renderer_version="pdfjs-4.2", raster=b"RASTER",
    )
    assert h1 == h2 and len(h1) == 64
    # A dpi change moves pixels → a different rendition hash.
    h_dpi = compute_rendition_hash(
        attachment_sha256="a" * 64, page=1, dpi=300,
        renderer_version="pdfjs-4.2", raster=b"RASTER",
    )
    assert h_dpi != h1
    # A renderer bump can move pixels → a different rendition hash.
    h_rend = compute_rendition_hash(
        attachment_sha256="a" * 64, page=1, dpi=150,
        renderer_version="pdfjs-4.3", raster=b"RASTER",
    )
    assert h_rend != h1


def test_freeze_stores_raster_and_returns_a_bound_record():
    blobs = _InMemoryBlobStore()
    rend = freeze_rendition(
        attachment_sha256="b" * 64, page=2, dpi=150,
        renderer_version="pdfjs-4.2", raster=b"PAGEBYTES", blob_store=blobs,
    )
    assert rend.rendition_hash and rend.page == 2 and rend.dpi == 150
    # The raster is retrievable from the object store under the rendition key.
    assert blobs.get_blob(rend.storage_key) == b"PAGEBYTES"


def test_verify_render_basis_accepts_matching_basis():
    rend = freeze_rendition(
        attachment_sha256="c" * 64, page=1, dpi=150,
        renderer_version="pdfjs-4.2", raster=b"R", blob_store=_InMemoryBlobStore(),
    )
    # Same basis → geometry stays valid (no raise).
    verify_render_basis(rend, dpi=150, renderer_version="pdfjs-4.2")


def test_verify_render_basis_rejects_changed_basis():
    rend = freeze_rendition(
        attachment_sha256="d" * 64, page=1, dpi=150,
        renderer_version="pdfjs-4.2", raster=b"R", blob_store=_InMemoryBlobStore(),
    )
    with pytest.raises(RenderBasisMismatch):
        verify_render_basis(rend, dpi=300, renderer_version="pdfjs-4.2")
    with pytest.raises(RenderBasisMismatch):
        verify_render_basis(rend, dpi=150, renderer_version="pdfjs-4.3")


def test_spatial_anchor_can_bind_a_rendition_hash():
    # ADR-045 P2.8 — a verified spatial anchor carries the rendition hash its
    # geometry was computed against; a text-derived anchor never does.
    from gateways.document_extraction import build_spatial_anchor
    from api.schemas import MatchKey

    rend = freeze_rendition(
        attachment_sha256="e" * 64, page=1, dpi=150,
        renderer_version="pdfjs-4.2", raster=b"R", blob_store=_InMemoryBlobStore(),
    )
    candidates = [{"candidate_id": 7, "page": 1, "bbox": [0.1, 0.1, 0.4, 0.2], "text": "PO-42"}]
    anchor = build_spatial_anchor(
        candidates=candidates, chosen_id=7, rendered_text_under_box="PO-42",
        attachment_id="att-1", text="PO-42",
        match_key=MatchKey(normalized_text="po-42", occurrence_index=0),
        supports_kind="extracted_field", supports_ref="order_entry.customer_po",
        label="PO number", source_sha256="e" * 64, confidence=0.9,
        rendition_hash=rend.rendition_hash,
    )
    assert anchor.anchor_source == "spatial_extracted"
    assert anchor.rendition_hash == rend.rendition_hash


def test_text_derived_anchor_rejects_rendition_hash():
    from pydantic import ValidationError
    from api.schemas import EvidenceAnchor, MatchKey

    with pytest.raises(ValidationError):
        EvidenceAnchor(
            attachment_id="att-1", anchor_source="text_derived", text="x",
            match_key=MatchKey(normalized_text="x", occurrence_index=0),
            supports_kind="extracted_field", supports_ref="order_entry.x",
            label="X", source_sha256="a" * 64, rendition_hash="deadbeef",
        )
