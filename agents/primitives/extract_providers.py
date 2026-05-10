"""ADR-038 §11.2 — multimodal extractor providers.

Production implementations of the `MultimodalProvider` Protocol
declared in `agents.primitives.extract_attachment`. The PO chose
Azure for the procurement decision (2026-05-09):

  * **Primary** — Azure AI Document Intelligence
    (`azure-ai-documentintelligence` SDK, prebuilt-document /
    prebuilt-layout / prebuilt-invoice models). Pay-per-page;
    enterprise-grade SLA; Azure Workload Identity friendly.

  * **Free fallback** — Chandra OCR (open-source HuggingFace
    model — `linkinrustle/OCR`, fork of Datalab's Chandra 2).
    Useful for lower-stakes extractions where the cost of
    Azure DI is unjustified, or as a soak-test backstop while
    DI quotas are being negotiated. Lazy import — heavy deps
    (PyTorch / Transformers / vLLM) only load when explicitly
    selected.

Selection: `ASOE_OCR_PRIMARY=azure_di | chandra | stub`. Default
`stub` keeps tests deterministic.

Both providers translate vendor-native output into the canonical
`List[ExtractedField]` shape; downstream agents see a uniform
contract regardless of which provider answered.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agents.primitives.extract_attachment import (
    AttachmentRef,
    ExtractedField,
    MultimodalProvider,
    StubMultimodalProvider,
)

logger = logging.getLogger("asoe.agents.extract_providers")


# ---------------------------------------------------------------------------
# Azure Document Intelligence — primary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AzureDIConfig:
    endpoint: str
    api_key: str
    model_id: str  # "prebuilt-document" / "prebuilt-invoice" / "prebuilt-layout"

    @classmethod
    def from_env(cls) -> "AzureDIConfig":
        return cls(
            endpoint=os.environ["AZURE_DI_ENDPOINT"].rstrip("/"),
            api_key=os.environ["AZURE_DI_API_KEY"],
            model_id=os.getenv("AZURE_DI_MODEL_ID", "prebuilt-document"),
        )


class AzureDocumentIntelligenceProvider:
    """`MultimodalProvider` impl backed by Azure AI Document
    Intelligence. Supports PDFs (native + scanned), images, and
    Office documents the prebuilt-* models accept.

    The provider does NOT enforce a particular extraction schema —
    every key/value pair the model returns becomes an
    `ExtractedField`. The agent's `fields_hint` argument is honoured
    when present (used to filter to the requested fields), but
    falls through to "return everything" when not.
    """

    provider_name = "azure_document_intelligence"

    def __init__(
        self,
        config: Optional[AzureDIConfig] = None,
        *,
        sdk_client: Any = None,
    ) -> None:
        self._config = config if config is not None else AzureDIConfig.from_env()
        self._client = sdk_client if sdk_client is not None else self._build_client()

    def _build_client(self) -> Any:
        try:
            from azure.ai.documentintelligence import DocumentIntelligenceClient  # noqa: PLC0415
            from azure.core.credentials import AzureKeyCredential  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — packaging issue
            raise RuntimeError(
                "Azure Document Intelligence provider requires "
                "`azure-ai-documentintelligence` and `azure-core`. "
                "Install with: pip install 'asoe[azure-di]'.",
            ) from exc
        return DocumentIntelligenceClient(
            endpoint=self._config.endpoint,
            credential=AzureKeyCredential(self._config.api_key),
        )

    # ----- Protocol surface --------------------------------------------

    def extract(
        self,
        attachment: AttachmentRef,
        fields_hint: Optional[List[str]] = None,
    ) -> List[ExtractedField]:
        """Run the prebuilt model against the attachment.

        The provider expects `attachment.bytes_locator` to point at a
        fetchable URL (Azure can fetch via SAS / HTTPS). For
        filesystem-resident attachments the harness uploads via the
        SDK's `bytes_source` path — that round-trip is the harness's
        concern, not this provider's.
        """
        if not attachment.bytes_locator:
            raise ValueError(
                f"Attachment {attachment.attachment_id} has no "
                "bytes_locator; Azure DI requires a fetchable URL.",
            )
        try:
            poller = self._client.begin_analyze_document(
                model_id=self._config.model_id,
                analyze_request={"urlSource": attachment.bytes_locator},
            )
            result = poller.result()
        except TimeoutError:
            raise
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status == 400:
                raise ValueError(str(exc)) from exc
            raise

        return self._translate(result, fields_hint=fields_hint)

    # ----- helpers ------------------------------------------------------

    @staticmethod
    def _translate(
        result: Any,
        *,
        fields_hint: Optional[List[str]],
    ) -> List[ExtractedField]:
        """Translate the AnalyzedDocument into ExtractedField list.

        DI returns key/value pairs under `result.key_value_pairs`
        (each with `key.content` / `value.content` / `confidence`).
        We project those one-to-one. The optional `documents` list
        carries higher-level field shapes from prebuilt-invoice; we
        also include those when present.
        """
        fields: List[ExtractedField] = []
        wanted = set(fields_hint or [])

        for kv in getattr(result, "key_value_pairs", None) or []:
            key_obj = getattr(kv, "key", None)
            val_obj = getattr(kv, "value", None)
            if key_obj is None or val_obj is None:
                continue
            name = (getattr(key_obj, "content", "") or "").strip()
            value = (getattr(val_obj, "content", "") or "").strip()
            confidence = float(getattr(kv, "confidence", 0.0) or 0.0)
            if not name:
                continue
            if wanted and name not in wanted:
                continue
            fields.append(
                ExtractedField(name=name, value=value, confidence=confidence),
            )

        # Higher-level prebuilt-invoice / prebuilt-receipt shapes —
        # iterate `documents[].fields` if present.
        for doc in getattr(result, "documents", None) or []:
            for field_name, field in (getattr(doc, "fields", {}) or {}).items():
                if wanted and field_name not in wanted:
                    continue
                # Prefer the typed value when DI extracted one.
                value = (
                    getattr(field, "content", None)
                    or getattr(field, "value_string", None)
                    or ""
                )
                confidence = float(getattr(field, "confidence", 0.0) or 0.0)
                fields.append(
                    ExtractedField(
                        name=field_name,
                        value=str(value).strip(),
                        confidence=confidence,
                    ),
                )

        return fields


# ---------------------------------------------------------------------------
# Chandra OCR — free fallback (lazy-import; heavy deps)
# ---------------------------------------------------------------------------


class ChandraOCRProvider:
    """`MultimodalProvider` impl backed by Chandra OCR (open source).

    Lazy-import — the heavy PyTorch / Transformers / vLLM deps only
    load when the provider is actually selected. This keeps the
    default test runtime dependency-free.

    Configuration (env):
      ASOE_CHANDRA_MODEL_PATH = <local model path>  (optional)
      ASOE_CHANDRA_DEVICE     = cpu | cuda | mps    (default cpu)

    Output shape: Chandra returns structured HTML / Markdown / JSON
    with key/value pairs preserved. We project the JSON variant into
    `ExtractedField` rows. When a request asks for fields not in the
    model output we omit them silently — same contract as Azure DI.
    """

    provider_name = "chandra_ocr"

    def __init__(self, *, model: Any = None) -> None:
        self._model = model  # injected for tests; built lazily otherwise
        self._device = os.getenv("ASOE_CHANDRA_DEVICE", "cpu")
        self._model_path = os.getenv("ASOE_CHANDRA_MODEL_PATH", "")

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            import chandra  # type: ignore[import-not-found]  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — soft dep
            raise RuntimeError(
                "Chandra OCR provider requires the `chandra` package "
                "(linkinrustle/OCR / Datalab Chandra 2 fork). Install "
                "with: pip install 'asoe[chandra-ocr]'.",
            ) from exc
        # The package exposes a `load_model` entry point that
        # respects the device + path env vars.
        self._model = chandra.load_model(
            model_path=self._model_path or None,
            device=self._device,
        )
        return self._model

    def extract(
        self,
        attachment: AttachmentRef,
        fields_hint: Optional[List[str]] = None,
    ) -> List[ExtractedField]:
        model = self._ensure_model()
        # Chandra accepts a path or URL; the AttachmentRef's
        # `bytes_locator` carries whichever the harness has. When
        # absent, fall back to the attachment_id as a path token —
        # the model's loader handles missing-file errors itself.
        source = attachment.bytes_locator or attachment.attachment_id
        try:
            output = model.process(
                source,
                output_format="json",
            )
        except TimeoutError:
            raise
        except Exception as exc:
            # Chandra surfaces its own exception types; we coarsely
            # classify `ValueError` as constrained-output defect.
            if isinstance(exc, ValueError):
                raise
            raise

        return self._translate(output, fields_hint=fields_hint)

    @staticmethod
    def _translate(
        output: Any,
        *,
        fields_hint: Optional[List[str]],
    ) -> List[ExtractedField]:
        """Translate Chandra's JSON output to ExtractedField rows.

        The expected shape is roughly::

            {"fields": {"name": {"value": "...", "confidence": 0.93}, ...},
             "tables": [...],  # not surfaced as ExtractedField
             "raw_text": "..."}
        """
        fields: List[ExtractedField] = []
        wanted = set(fields_hint or [])

        raw = output if isinstance(output, dict) else {}
        for name, blob in (raw.get("fields") or {}).items():
            if wanted and name not in wanted:
                continue
            if isinstance(blob, dict):
                value = str(blob.get("value", ""))
                confidence = float(blob.get("confidence", 0.0) or 0.0)
            else:
                value = str(blob)
                confidence = 0.5  # default when Chandra didn't score
            fields.append(
                ExtractedField(name=name, value=value, confidence=confidence),
            )
        return fields


# ---------------------------------------------------------------------------
# Factory — env-driven selection
# ---------------------------------------------------------------------------


def select_multimodal_provider() -> MultimodalProvider:
    """Pick the provider based on `ASOE_OCR_PRIMARY`.

    Values: ``azure_di`` / ``chandra`` / ``stub`` (default).
    Operators flip the env var to roll between vendors without a
    code change. Tests use the default `stub` value so they remain
    network-free.
    """
    primary = os.getenv("ASOE_OCR_PRIMARY", "stub").strip().lower()
    if primary == "azure_di":
        return AzureDocumentIntelligenceProvider()
    if primary == "chandra":
        return ChandraOCRProvider()
    return StubMultimodalProvider()
