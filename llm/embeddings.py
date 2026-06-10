"""Embedding provider for precedent retrieval (sign-off 2026-06-10).

Separate from the chat/tool-call provider protocol on purpose: the
precedents feature needs ONLY `embed`, and embeddings are advisory
retrieval — never a control field — so they sit outside the
constrained-generation machinery (CLAUDE.md Guardrail #3 governs
machine-consumed *generated* outputs; a similarity ranking consumed by
a human-facing evidence card is neither).

Configuration:
  ASOE_EMBEDDING_PROVIDER   "openai" enables the OpenAI embedder;
                            unset/empty disables semantic matching and
                            the precedents composer falls back to the
                            deterministic correlate path.
  ASOE_EMBEDDING_MODEL      model id; default "text-embedding-3-small"
                            (1536 dims — matches the V001
                            `exceptions.context_embedding VECTOR(1536)`
                            column).

`get_embedder()` returns None when unconfigured or when the SDK is
missing — callers treat None as "semantic path unavailable" and the
feature degrades to the correlate fallback, never an error.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional, Protocol, Sequence

logger = logging.getLogger(__name__)

DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"


class Embedder(Protocol):
    """Minimal embedding interface. `model` identifies the exact model
    for audit provenance (PrecedentCase.embedding_model)."""

    model: str

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        """Return one vector per input text, in input order."""
        ...


class OpenAIEmbedder:
    """OpenAI embeddings client. Lazy-imports the SDK so the core
    service runs without the dependency when the provider is off."""

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model or os.environ.get(
            "ASOE_EMBEDDING_MODEL", DEFAULT_OPENAI_EMBEDDING_MODEL
        )
        from openai import OpenAI  # lazy: optional dependency

        self._client = OpenAI()

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        response = self._client.embeddings.create(
            model=self.model, input=list(texts)
        )
        # The API returns items with an `index` field; sort to guarantee
        # input order regardless of response ordering.
        ordered = sorted(response.data, key=lambda d: d.index)
        return [d.embedding for d in ordered]


def get_embedder() -> Optional[Embedder]:
    """Resolve the configured embedder, or None (semantic path off).

    Misconfiguration (unknown provider, missing SDK) logs and returns
    None rather than raising: precedents must degrade to the correlate
    fallback, never take the analysis endpoint down.
    """
    provider = (os.environ.get("ASOE_EMBEDDING_PROVIDER") or "").strip().lower()
    if not provider:
        return None
    if provider == "openai":
        try:
            return OpenAIEmbedder()
        except Exception as exc:  # SDK missing / no API key at import
            logger.warning("Embedding provider 'openai' unavailable: %s", exc)
            return None
    logger.warning("Unknown ASOE_EMBEDDING_PROVIDER %r — semantic matching off", provider)
    return None
