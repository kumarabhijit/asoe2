"""ADR-038 Phase H.4 — attachment-extractor L2 primitive.

Single tool from the agent's POV — `extract_attachment(attachment_id,
fields_hint?)`. Internally:

  1. Fingerprint the document (template hash; same template across
     cases hits cache).
  2. Cache lookup keyed on ``(tenant_id, fingerprint)`` per ADR-038
     §5.8 — strict tenant isolation.
  3. Format dispatch — native PDF / scanned PDF / Excel / image /
     plain text.
  4. Extract structured fields with per-field confidence.
  5. Persist to cache + return.

The actual multimodal LLM call is gated on `MultimodalProvider` —
tests use the stub provider so the primitive is exercisable
without network. Production wires Anthropic / Azure / hybrid per
ADR-038 §10.8 procurement decision; the protocol below is the
single contract callers depend on.

Cost discipline (ADR-038 §8.2): the dominant cost in a Case Agent
run is the multimodal call. Cache discipline is the lever — repeat
customer templates hit cache and pay $0.

This module never imports from the L3 Case Agent or the L4 harness.
It is a pure primitive that primitive callers can use independently
of the agent loop.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Dict, List, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Typed contracts
# ---------------------------------------------------------------------------


AttachmentFormat = Literal[
    "native_pdf",      # text-extractable PDF (cheap text path)
    "scanned_pdf",     # OCR + LLM
    "excel",           # openpyxl + LLM structured output
    "image",           # multimodal LLM call
    "plain_text",      # cheapest path; structured output from raw text
    "unknown",
]


class AttachmentRef(BaseModel):
    """Pointer to an attachment — supplied by the case context.

    The bytes themselves are NOT loaded into the agent's working
    memory; only the structured ``ExtractedFields`` are persisted
    on the case. This keeps the agent's context bounded
    (ADR-038 §5.3 cache discipline).
    """

    model_config = ConfigDict(extra="forbid")

    attachment_id: str
    tenant_id: str
    case_id: Optional[str] = None
    name: str
    mime_type: str
    bytes: int
    # Hash of the document layout used for template-fingerprint cache.
    # In production this is a perceptual hash for images / a structural
    # hash for PDFs (table layout / form fields). The stub provider
    # returns a stable hash from a hand-supplied "template_id" so tests
    # can exercise both cache hits and misses deterministically.
    template_fingerprint: str
    # Optional pointer where the bytes live (filesystem path / S3 URI /
    # blob storage handle). The stub provider uses a sentinel string;
    # production paths plug their storage adapter here.
    bytes_locator: Optional[str] = None


class ExtractedField(BaseModel):
    """One structured field extracted from the attachment."""

    model_config = ConfigDict(extra="forbid")

    name: str
    value: Any
    confidence: float = Field(ge=0.0, le=1.0)


class ExtractedFields(BaseModel):
    """Output of `extract_attachment`. The agent sees only this — never
    the raw bytes."""

    model_config = ConfigDict(extra="forbid")

    attachment_id: str
    format: AttachmentFormat
    fields: List[ExtractedField]
    raw_excerpt: Optional[str] = None
    """Optional first-N-chars of the canonicalised text for human
    debugging. Bounded to 512 chars; never PII-bearing in production
    (redaction policy sits in front of this)."""
    cache_hit: bool = False


# ---------------------------------------------------------------------------
# Multimodal provider protocol — vendor-bound, mockable
# ---------------------------------------------------------------------------


class MultimodalProvider(Protocol):
    """The single boundary the extractor crosses to call a multimodal
    or text-LLM model. Production implementations: Anthropic (Claude
    Vision), Azure Document Intelligence, AWS Textract, local Ollama
    LLaVA. Tests use ``StubMultimodalProvider`` below.
    """

    def extract(
        self,
        attachment: AttachmentRef,
        fields_hint: Optional[List[str]] = None,
    ) -> List[ExtractedField]:
        """Return structured fields. Raises on protocol violation;
        the wrapper translates exceptions into the audit trail."""
        ...


class StubMultimodalProvider:
    """Deterministic stub — returns canned fields per
    ``template_fingerprint``. CI fixtures.

    Tests register fixtures with ``register_fixture(fingerprint,
    fields)``. A request whose fingerprint isn't registered returns
    a single low-confidence field naming the missing fixture (so
    the test failure points at the gap).
    """

    def __init__(self) -> None:
        self._fixtures: Dict[str, List[ExtractedField]] = {}

    def register_fixture(
        self,
        template_fingerprint: str,
        fields: List[ExtractedField],
    ) -> None:
        self._fixtures[template_fingerprint] = list(fields)

    def extract(
        self,
        attachment: AttachmentRef,
        fields_hint: Optional[List[str]] = None,
    ) -> List[ExtractedField]:
        fixture = self._fixtures.get(attachment.template_fingerprint)
        if fixture is not None:
            # Defensive copy so callers can't mutate the canned result.
            return [ExtractedField(**f.model_dump()) for f in fixture]
        return [
            ExtractedField(
                name="__stub_unmatched__",
                value=f"no fixture for fingerprint={attachment.template_fingerprint!r}",
                confidence=0.0,
            )
        ]


# ---------------------------------------------------------------------------
# Cache (per-tenant; in-memory)
# ---------------------------------------------------------------------------
#
# ADR-038 §5.8 binding rule: cache key includes ``tenant_id`` even
# when template fingerprints are byte-identical across tenants.
# Cross-tenant data isolation is non-negotiable (SOX requirement).


class ExtractionCache:
    """Thread-safe in-memory cache for ``ExtractedFields`` keyed by
    ``(tenant_id, template_fingerprint)``.

    Production wires a Redis-backed store with TTL (24h default per
    ADR-039 §5.5 cache discipline applied here too). The in-memory
    store has no TTL — tests clear() between runs.
    """

    def __init__(self) -> None:
        self._cache: Dict[tuple, ExtractedFields] = {}
        self._lock = threading.RLock()

    def _key(self, tenant_id: str, template_fingerprint: str) -> tuple:
        return (tenant_id, template_fingerprint)

    def get(
        self,
        tenant_id: str,
        template_fingerprint: str,
    ) -> Optional[ExtractedFields]:
        with self._lock:
            return self._cache.get(self._key(tenant_id, template_fingerprint))

    def put(
        self,
        tenant_id: str,
        template_fingerprint: str,
        fields: ExtractedFields,
    ) -> None:
        with self._lock:
            self._cache[self._key(tenant_id, template_fingerprint)] = fields

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._cache)


# Module-level singleton — tests reset via ``extraction_cache.clear()``.
extraction_cache = ExtractionCache()


# ---------------------------------------------------------------------------
# The extract_attachment primitive
# ---------------------------------------------------------------------------


def _infer_format(mime_type: str) -> AttachmentFormat:
    mt = (mime_type or "").lower()
    if mt == "application/pdf":
        return "native_pdf"  # caller can override to scanned_pdf via metadata
    if mt in ("application/vnd.ms-excel",
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
        return "excel"
    if mt.startswith("image/"):
        return "image"
    if mt.startswith("text/"):
        return "plain_text"
    return "unknown"


def extract_attachment(
    attachment: AttachmentRef,
    *,
    fields_hint: Optional[List[str]] = None,
    provider: MultimodalProvider,
    cache: ExtractionCache = extraction_cache,
) -> ExtractedFields:
    """L2 primitive — extract structured fields from one attachment.

    Returns an ``ExtractedFields`` with ``cache_hit=True`` when the
    request was served from the per-tenant cache. ``fields_hint`` is
    forwarded to the provider for prompt narrowing; the cache key
    intentionally does NOT include it so the same template hits
    cache regardless of hint variations across cases.

    Raises nothing on extraction failure — the provider may return a
    field named ``"__error__"`` with a human reason; callers reason
    about that as an observation per the L3 agent contract
    (ADR-038 §6.3 "errors flow back as observations").
    """
    # 1. Cache lookup (per-tenant per ADR-038 §5.8).
    cached = cache.get(attachment.tenant_id, attachment.template_fingerprint)
    if cached is not None:
        return cached.model_copy(update={
            "attachment_id": attachment.attachment_id,
            "cache_hit": True,
        })

    # 2. Format dispatch.
    fmt = _infer_format(attachment.mime_type)

    # 3. Provider call (the only place vendor-bound code lives).
    fields = provider.extract(attachment, fields_hint=fields_hint)

    # 4. Build the result.
    raw_excerpt: Optional[str] = None
    # In production the format-dispatch path emits a 512-char excerpt;
    # the stub provider doesn't carry one.

    result = ExtractedFields(
        attachment_id=attachment.attachment_id,
        format=fmt,
        fields=fields,
        raw_excerpt=raw_excerpt,
        cache_hit=False,
    )

    # 5. Persist to cache. The cached entry intentionally retains
    # cache_hit=False — the next caller flips it on retrieval.
    cache.put(attachment.tenant_id, attachment.template_fingerprint, result)
    return result


def fingerprint_for_template(template_id: str) -> str:
    """Helper for tests / fixtures to produce a stable fingerprint
    from a human-readable template id. Production paths use a
    perceptual hash; this just SHA-256s the id so tests have a
    predictable fingerprint they can register against."""
    return hashlib.sha256(template_id.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Helper: build an AttachmentRef from raw metadata
# ---------------------------------------------------------------------------


def attachment_ref_from_metadata(
    tenant_id: str,
    case_id: Optional[str],
    raw: Dict[str, Any],
) -> AttachmentRef:
    """Convert a metadata dict (e.g. from event.metadata.attachment_manifest
    entries) into a typed AttachmentRef. Defensive on missing fields."""
    return AttachmentRef(
        attachment_id=str(raw.get("attachment_id") or raw.get("name") or ""),
        tenant_id=tenant_id,
        case_id=case_id,
        name=str(raw.get("name") or ""),
        mime_type=str(raw.get("mime_type") or ""),
        bytes=int(raw.get("bytes") or 0),
        template_fingerprint=str(
            raw.get("template_fingerprint")
            or fingerprint_for_template(str(raw.get("name") or "unknown"))
        ),
        bytes_locator=raw.get("bytes_locator"),
    )
