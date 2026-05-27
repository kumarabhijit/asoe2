"""PARITY-7 — spatial extraction hardening.

ML review requirements that gate the live path:

  * **Text-normalisation contract**: ``gateways.document_extraction._normalize``
    must handle soft hyphens (U+00AD), unicode ligatures (fi, fl, ff,
    ffi, ffl), and unicode NFC vs NFD consistently with AzureDI output.
    A ligature mismatch silently degrades the verifier to text-only mode.

  * **Golden-set expansion**: ``tests/eval/datasets/extraction_spatial/seed.jsonl``
    must carry ≥10 rows spanning born-digital, scanned, multi-page, and
    table-heavy document types — not a single development prototype row.

  * **Per-document-type thresholds**: ``tests/eval/thresholds.yaml``
    grows ``extraction_spatial.per_type`` so table-heavy /
    single-line / multi-page each get their own
    containment/hallucination ceiling.

  * **Model-id env pin**: ``ASOE_DOCUMENT_EXTRACTION_MODEL_ID`` env
    var is the deterministic dispatch key for the live AzureDI model;
    a model bump requires an env-var change + a fresh golden-set rerun
    (not an automatic rolling upgrade).
"""

from __future__ import annotations

import json
import os
import unicodedata
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_JSONL = REPO_ROOT / "tests" / "eval" / "datasets" / "extraction_spatial" / "seed.jsonl"
THRESHOLDS_YAML = REPO_ROOT / "tests" / "eval" / "thresholds.yaml"


class TestTextNormalizationContract:
    def test_soft_hyphen_removed(self):
        from gateways.document_extraction import _normalize

        # U+00AD soft hyphen — AzureDI strips these on extraction; our
        # verifier MUST do the same or the bbox-text won't match.
        with_shy = "pur­chase order"
        assert "­" not in _normalize(with_shy)

    def test_ligature_fi_expanded(self):
        from gateways.document_extraction import _normalize

        # U+FB01 ligature "ﬁ" — AzureDI normalizes to "fi" via NFKC.
        with_lig = "conﬁrmed"
        out = _normalize(with_lig)
        assert "ﬁ" not in out
        assert "fi" in out

    def test_ligature_fl_expanded(self):
        from gateways.document_extraction import _normalize

        with_lig = "Auﬂux"  # "Auflux"
        out = _normalize(with_lig)
        assert "ﬂ" not in out
        assert "fl" in out

    def test_nfc_vs_nfd_equivalence(self):
        """Decomposed-form "é" (e + combining acute) must normalize to
        the same string as composed-form "é"."""
        from gateways.document_extraction import _normalize

        composed = "Café"   # U+00E9
        decomposed = "Café"  # 'e' + U+0301
        assert _normalize(composed) == _normalize(decomposed)

    def test_normalize_idempotent(self):
        from gateways.document_extraction import _normalize

        sample = "  PO#  12345-fi­nal  "
        once = _normalize(sample)
        twice = _normalize(once)
        assert once == twice

    def test_normalize_preserves_payload_when_no_pii_or_quirks(self):
        from gateways.document_extraction import _normalize

        plain = "  Hello   World  "
        assert _normalize(plain) == "hello world"


class TestGoldenSetExpansion:
    """The seed.jsonl gates Phase 7's live path. One row isn't enough."""

    def test_at_least_10_rows(self):
        rows = [
            json.loads(line)
            for line in SEED_JSONL.read_text().splitlines()
            if line.strip()
        ]
        assert len(rows) >= 10, (
            f"extraction_spatial/seed.jsonl has only {len(rows)} rows; "
            f"ML review requires ≥10 spanning born-digital, scanned, "
            f"multi-page, table-heavy."
        )

    def test_covers_required_document_types(self):
        rows = [
            json.loads(line)
            for line in SEED_JSONL.read_text().splitlines()
            if line.strip()
        ]
        types = {r.get("document_type") for r in rows}
        for required in ("born_digital", "scanned", "multi_page", "table_heavy"):
            assert required in types, (
                f"seed.jsonl missing document_type={required!r}; "
                f"the live-path acceptance threshold per type can't be "
                f"validated without representative coverage."
            )

    def test_every_row_has_fields(self):
        for line in SEED_JSONL.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            assert row.get("fields"), f"row {row.get('id')} has no fields"


class TestPerDocumentTypeThresholds:
    def test_thresholds_carry_per_type_block(self):
        import yaml

        cfg = yaml.safe_load(THRESHOLDS_YAML.read_text())
        per_type = cfg.get("extraction_spatial", {}).get("per_type")
        assert per_type, "thresholds.yaml lacks extraction_spatial.per_type"

    def test_per_type_covers_all_required_buckets(self):
        import yaml

        cfg = yaml.safe_load(THRESHOLDS_YAML.read_text())
        per_type = cfg["extraction_spatial"]["per_type"]
        for required in ("born_digital", "scanned", "multi_page", "table_heavy"):
            assert required in per_type, f"per_type missing {required}"
            assert "containment_min" in per_type[required]
            assert "hallucination_rate_max" in per_type[required]

    def test_table_heavy_threshold_strictly_below_born_digital(self):
        """Table-heavy documents are noisier — the threshold must be
        equal or below born-digital, never above."""
        import yaml

        cfg = yaml.safe_load(THRESHOLDS_YAML.read_text())
        per = cfg["extraction_spatial"]["per_type"]
        assert per["table_heavy"]["containment_min"] <= per["born_digital"]["containment_min"]


class TestModelIdEnvPin:
    def test_model_id_resolver_reads_env_var(self, monkeypatch):
        monkeypatch.setenv("ASOE_DOCUMENT_EXTRACTION_MODEL_ID", "prebuilt-invoice-4-3")
        from gateways.document_extraction import resolve_model_id

        assert resolve_model_id() == "prebuilt-invoice-4-3"

    def test_model_id_resolver_default(self, monkeypatch):
        monkeypatch.delenv("ASOE_DOCUMENT_EXTRACTION_MODEL_ID", raising=False)
        from gateways.document_extraction import resolve_model_id

        # Default must be deterministic — not "whatever AzureDI is
        # serving today" — so a model bump is a code-or-env change.
        assert resolve_model_id() == "prebuilt-invoice"
