"""Frozen page renditions (ADR-044 §2.5) — the render basis spatial geometry binds to.

A spatial `bbox` (ADR-045) is only meaningful against the exact page render it was
computed on: re-rendering (PDF.js bump, office→PDF convert, a dpi change) moves
pixels. A *frozen rendition* pins that basis — the raster bytes + `dpi` +
`renderer_version` (+ the source attachment `sha256` and page) — and hashes them.
The raster is stored in the object store; the hash is the binding an
`EvidenceAnchor` carries (`rendition_hash`).

Coordinate-validity invariant: geometry computed against rendition H is valid
only when re-rendered under H's basis. A `dpi` / `renderer_version` change without
a *new* rendition hash is a **hard failure** (`RenderBasisMismatch`) — never a
silently-misplaced box.

This is minimal — just enough for ADR-045 geometry to be verifiable. Rendition
lifecycle (erasure-cascade, retention) is governance and out of scope here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from gateways.attachment_store import _BlobStore, _InMemoryBlobStore


class RenderBasisMismatch(Exception):
    """Raised when geometry is checked against a render basis (dpi /
    renderer_version) that differs from the frozen rendition it was computed on."""


@dataclass(frozen=True)
class FrozenRendition:
    rendition_hash: str
    attachment_sha256: str
    page: int
    dpi: int
    renderer_version: str
    storage_key: str


def compute_rendition_hash(
    *, attachment_sha256: str, page: int, dpi: int, renderer_version: str, raster: bytes,
) -> str:
    """Deterministic content+basis hash. Any change to the raster OR the basis
    (dpi / renderer_version / page / source bytes) yields a different hash, so a
    re-render under a moved basis cannot reuse an old rendition identity."""
    h = hashlib.sha256()
    # Length-prefixed fields so concatenation is unambiguous.
    for part in (attachment_sha256, str(page), str(dpi), renderer_version):
        encoded = part.encode("utf-8")
        h.update(len(encoded).to_bytes(4, "big"))
        h.update(encoded)
    h.update(len(raster).to_bytes(8, "big"))
    h.update(raster)
    return h.hexdigest()


def _storage_key(attachment_sha256: str, page: int, rendition_hash: str) -> str:
    return f"renditions/{attachment_sha256}/p{page}/{rendition_hash}"


def freeze_rendition(
    *,
    attachment_sha256: str,
    page: int,
    dpi: int,
    renderer_version: str,
    raster: bytes,
    blob_store: Optional[_BlobStore] = None,
) -> FrozenRendition:
    """Store the page raster in the object store and return its bound record. The
    rendition hash is the basis identity an `EvidenceAnchor.rendition_hash`
    references; the raster is retrievable for re-render verification."""
    store = blob_store if blob_store is not None else _InMemoryBlobStore()
    rendition_hash = compute_rendition_hash(
        attachment_sha256=attachment_sha256, page=page, dpi=dpi,
        renderer_version=renderer_version, raster=raster,
    )
    key = _storage_key(attachment_sha256, page, rendition_hash)
    store.put_blob(key, raster)
    return FrozenRendition(
        rendition_hash=rendition_hash, attachment_sha256=attachment_sha256,
        page=page, dpi=dpi, renderer_version=renderer_version, storage_key=key,
    )


def verify_render_basis(
    rendition: FrozenRendition, *, dpi: int, renderer_version: str,
) -> None:
    """The coordinate-validity invariant. Raises ``RenderBasisMismatch`` when the
    supplied basis differs from the rendition's — geometry is only valid against
    the basis it was frozen on."""
    if rendition.dpi != dpi or rendition.renderer_version != renderer_version:
        raise RenderBasisMismatch(
            f"render basis changed: frozen=(dpi={rendition.dpi}, "
            f"renderer={rendition.renderer_version!r}) vs "
            f"(dpi={dpi}, renderer={renderer_version!r}) — re-freeze the rendition"
        )
