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

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from api.schemas import EvidenceAnchor, MatchKey

logger = logging.getLogger("asoe.gateways.document_extraction")

GATEWAY_NAME = "document_extraction"


def _normalize(s: Any) -> str:
    """Shared text-normalisation contract — PARITY-7 (ML review).

    Used by:
      * the spatial verifier when matching anchor text → OCR span
      * any code path comparing model-extracted text to ground truth

    The contract MUST match AzureDI's pre-OCR normalisation, otherwise
    the verifier silently degrades to text-only mode on the first
    document containing a ligature or soft hyphen.

    Steps (order matters):
      1. NFKC normalisation — composes accents AND expands compatibility
         ligatures (U+FB01 "ﬁ" → "fi", U+FB02 "ﬂ" → "fl", …).
      2. Soft-hyphen (U+00AD) stripped — these are invisible line-break
         hints; AzureDI drops them on extraction.
      3. Whitespace collapsed to single spaces.
      4. Casefold for case-insensitive comparison.
    """
    import unicodedata
    if s is None:
        return ""
    text = unicodedata.normalize("NFKC", str(s))
    text = text.replace("­", "")  # soft hyphen
    text = " ".join(text.split())
    return text.casefold()


def resolve_model_id() -> str:
    """Return the AzureDI model id this deploy is pinned to.

    Read from ``ASOE_DOCUMENT_EXTRACTION_MODEL_ID``. A model bump
    requires a deliberate env-var change AND a fresh golden-set rerun
    (ML review): we never want AzureDI's silent rolling upgrade to
    move bounding boxes on us mid-quarter.

    Default ``prebuilt-invoice`` is the safe baseline established by
    Decision Q2 — prebuilt invoice + custom post-process. Phase 7
    canary acceptance gates any upgrade to custom-extract.
    """
    return (os.getenv("ASOE_DOCUMENT_EXTRACTION_MODEL_ID") or "prebuilt-invoice").strip()


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
            "rendition_hash": None,
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
    rendition_hash: Optional[str] = None,
) -> EvidenceAnchor:
    """Build a VERIFIED spatial anchor: SELECT a candidate box (never generate
    one), construct the anchor, then run the verifier. Returns a spatial anchor
    when verified, else a degraded text anchor (geometry is not required for
    audit). This is the single place a spatial anchor is minted. ``rendition_hash``
    binds the geometry to the frozen render basis it was computed on (ADR-044 §2.5).
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
        rendition_hash=rendition_hash,
    )
    return verify_anchor_geometry(anchor, rendered_text_under_box)


# ---------------------------------------------------------------------------
# Match-key computation (mirrors api.analysis_adapters.compute_match_keys; kept
# local to avoid importing the heavy adapter module / a circular dependency).
# ---------------------------------------------------------------------------

def _compute_match_keys(values: Sequence[str]) -> List[MatchKey]:
    seen: Dict[str, int] = {}
    keys: List[MatchKey] = []
    for v in values:
        norm = _normalize(v)
        idx = seen.get(norm, 0)
        seen[norm] = idx + 1
        keys.append(MatchKey(normalized_text=norm, occurrence_index=idx))
    return keys


# ---------------------------------------------------------------------------
# Backend seam (ADR-045 §2.3). Default = RecordedDocumentExtractionBackend
# replaying a frozen candidate set; a real managed-OCR / self-hosted layout
# provider implements the same `propose` surface on a live/nightly path.
# ---------------------------------------------------------------------------

_FIXTURE_ROOT = (
    Path(__file__).resolve().parent.parent
    / "tests" / "fixtures" / "gateway" / "document_extraction"
)

_CASE_HINT_KEYS = ("case", "recording_case", "order_id")


class RecordedDocumentExtractionBackend:
    """Replays a frozen OCR candidate set + field selections for deterministic
    TDD. Recordings live under
    ``tests/fixtures/gateway/document_extraction/<case>.recorded.json`` and carry
    ``model_id`` / ``prompt_hash`` provenance. A missing recording fails loud —
    never fabricates geometry (Guardrail #5/#6)."""

    def __init__(self, recordings_dir: "Path | str | None" = None) -> None:
        base = Path(recordings_dir) if recordings_dir is not None else _FIXTURE_ROOT
        self._dir = base
        self._by_case: Dict[str, dict] = {}
        if base.is_dir():
            for path in sorted(base.glob("*.recorded.json")):
                rec = json.loads(path.read_text())
                case = rec.get("case")
                if case:
                    self._by_case[str(case)] = rec

    def _resolve_case(self, hint: Mapping[str, Any]) -> str:
        for key in _CASE_HINT_KEYS:
            value = hint.get(key)
            if value:
                return str(value)
        raise KeyError(
            "RecordedDocumentExtractionBackend: request carries no case hint "
            f"(looked for {_CASE_HINT_KEYS})"
        )

    @property
    def model_id(self) -> str:
        # Provenance is per-recording; the default id is used when a recording
        # doesn't pin one. The real backend reports its deployed model id.
        return "recorded-doctr-v0"

    def propose(self, *, source_sha256: str, hint: Mapping[str, Any]) -> dict:  # noqa: ARG002
        case = self._resolve_case(hint)
        rec = self._by_case.get(case)
        if rec is None:
            raise KeyError(
                f"No recorded document-extraction output for case {case!r} under "
                f"{self._dir} (have: {sorted(self._by_case)})"
            )
        return rec


class DocumentExtractionGateway:
    """Mints verified spatial EvidenceAnchors from an OCR candidate set
    (ADR-045 §2.3). SELECT-not-generate + runtime verify via `build_spatial_anchor`.

    * **Idempotent** keyed on ``(source_sha256, model_id)`` (ADR-044 content
      address) — re-processing the same bytes with the same model replays the
      cached anchors byte-identically (free dedup + an audit fact).
    * **Circuit-breaker parity** with the other gateways: an OPEN breaker (OCR
      outage) degrades to ``[]`` so the composer falls back to ADR-043 text
      anchors — geometry is never required for audit and never blocks preview.
    """

    def __init__(self, backend: Optional[RecordedDocumentExtractionBackend] = None) -> None:
        self._backend = backend if backend is not None else RecordedDocumentExtractionBackend()
        self._lock = threading.Lock()
        self._cache: Dict[tuple, List[EvidenceAnchor]] = {}

    def _model_id(self, recording: Mapping[str, Any]) -> str:
        return str(recording.get("model_id") or getattr(self._backend, "model_id", "unknown"))

    def extract_anchors(
        self,
        *,
        attachment_id: str,
        source_sha256: str,
        hint: Mapping[str, Any],
        rendition_hash: Optional[str] = None,
    ) -> List[EvidenceAnchor]:
        # Circuit-breaker gate (DoR #8 parity). OPEN → degrade to no geometry.
        from gateways.circuit_breaker import CircuitOpen, get_breaker  # noqa: PLC0415

        breaker = get_breaker(GATEWAY_NAME)
        try:
            breaker.acquire()
        except CircuitOpen:
            logger.warning("document_extraction breaker OPEN — degrading to text anchors")
            return []

        try:
            recording = self._backend.propose(source_sha256=source_sha256, hint=hint)
        except KeyError:
            # A missing recording is a determinism failure, not a runtime outage —
            # surface it (and don't trip the breaker on a fixture gap).
            breaker.record_success(0.0)
            raise
        except Exception as exc:  # pragma: no cover - real-provider outage path
            breaker.record_failure(0.0)
            logger.warning("document_extraction provider failed (%s) — degrading", exc)
            return []
        breaker.record_success(0.0)

        model_id = self._model_id(recording)
        cache_key = (source_sha256, model_id, rendition_hash)
        with self._lock:
            cached = self._cache.get(cache_key)
        if cached is not None:
            return [a.model_copy(deep=True) for a in cached]

        candidates = recording.get("candidates") or []
        fields = recording.get("fields") or []
        match_keys = _compute_match_keys([str(f.get("text", "")) for f in fields])

        anchors: List[EvidenceAnchor] = []
        for field, mk in zip(fields, match_keys):
            chosen_id = field.get("chosen_id")
            box = select_candidate_box(candidates, chosen_id)
            anchors.append(build_spatial_anchor(
                candidates=candidates,
                chosen_id=chosen_id,
                rendered_text_under_box=box.get("text"),
                attachment_id=attachment_id,
                text=str(field.get("text", "")),
                match_key=mk,
                supports_kind="extracted_field",
                supports_ref=str(field.get("supports_ref", "")),
                label=str(field.get("label", "")),
                source_sha256=source_sha256,
                confidence=field.get("confidence"),
                rendition_hash=rendition_hash,
            ))

        with self._lock:
            self._cache[cache_key] = [a.model_copy(deep=True) for a in anchors]
        return anchors


__all__: List[str] = [
    "GATEWAY_NAME",
    "select_candidate_box",
    "verify_anchor_geometry",
    "build_spatial_anchor",
    "RecordedDocumentExtractionBackend",
    "DocumentExtractionGateway",
]
