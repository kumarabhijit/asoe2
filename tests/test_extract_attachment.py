"""ADR-038 Phase H.4 — attachment extractor tests.

Locks the L2 primitive's contracts:
  * Format dispatch infers from MIME type.
  * Provider returns structured fields with per-field confidence.
  * Cache hit short-circuits the provider; ``cache_hit=True`` flag set.
  * Tenant isolation — same fingerprint, different tenants → cache miss
    on the second tenant (ADR-038 §5.8 binding).
  * AttachmentRef helper coerces from raw metadata robustly.
"""

from __future__ import annotations

import pytest

from agents.primitives.extract_attachment import (
    AttachmentRef,
    ExtractedField,
    ExtractedFields,
    StubMultimodalProvider,
    attachment_ref_from_metadata,
    extract_attachment,
    extraction_cache,
    fingerprint_for_template,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    extraction_cache.clear()
    yield
    extraction_cache.clear()


@pytest.fixture
def provider():
    p = StubMultimodalProvider()
    # Register a fixture for a representative customer template.
    p.register_fixture(
        fingerprint_for_template("kroger_po_v3"),
        [
            ExtractedField(name="po_number", value="KRO-2025-03-44821", confidence=0.99),
            ExtractedField(name="ship_to", value="Kroger DC #5", confidence=0.95),
            ExtractedField(name="line_count", value=5, confidence=0.97),
            ExtractedField(name="total_amount", value=18400.0, confidence=0.92),
        ],
    )
    return p


def _ref(tenant_id: str = "t1", template_id: str = "kroger_po_v3", **overrides) -> AttachmentRef:
    base = dict(
        attachment_id="att-001",
        tenant_id=tenant_id,
        name="po.pdf",
        mime_type="application/pdf",
        bytes=12345,
        template_fingerprint=fingerprint_for_template(template_id),
    )
    base.update(overrides)
    return AttachmentRef(**base)


# ---------------------------------------------------------------------------
# Format dispatch
# ---------------------------------------------------------------------------


class TestFormatDispatch:
    def test_pdf_routes_to_native_pdf(self, provider):
        result = extract_attachment(_ref(mime_type="application/pdf"), provider=provider)
        assert result.format == "native_pdf"

    def test_excel_routes_to_excel(self, provider):
        result = extract_attachment(
            _ref(
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                template_fingerprint=fingerprint_for_template("kroger_po_v3"),
            ),
            provider=provider,
        )
        assert result.format == "excel"

    def test_image_routes_to_image(self, provider):
        result = extract_attachment(
            _ref(mime_type="image/png"), provider=provider,
        )
        assert result.format == "image"

    def test_text_routes_to_plain_text(self, provider):
        result = extract_attachment(
            _ref(mime_type="text/plain"), provider=provider,
        )
        assert result.format == "plain_text"

    def test_unknown_mime_routes_to_unknown(self, provider):
        result = extract_attachment(
            _ref(mime_type="application/octet-stream"), provider=provider,
        )
        assert result.format == "unknown"


# ---------------------------------------------------------------------------
# Provider integration
# ---------------------------------------------------------------------------


class TestProviderIntegration:
    def test_extracted_fields_carry_confidence(self, provider):
        result = extract_attachment(_ref(), provider=provider)
        assert len(result.fields) == 4
        for field in result.fields:
            assert 0.0 <= field.confidence <= 1.0

    def test_unmatched_fingerprint_returns_low_confidence_marker(self, provider):
        # A fingerprint we never registered — provider returns the
        # "__stub_unmatched__" marker so the test sees the gap.
        result = extract_attachment(
            _ref(template_fingerprint=fingerprint_for_template("never-seen")),
            provider=provider,
        )
        assert len(result.fields) == 1
        assert result.fields[0].name == "__stub_unmatched__"
        assert result.fields[0].confidence == 0.0


# ---------------------------------------------------------------------------
# Cache (in-tenant + tenant-isolation)
# ---------------------------------------------------------------------------


class TestCache:
    def test_first_call_misses_cache(self, provider):
        result = extract_attachment(_ref(), provider=provider)
        assert result.cache_hit is False

    def test_second_call_same_tenant_same_fingerprint_hits_cache(self, provider):
        first = extract_attachment(_ref(), provider=provider)
        assert first.cache_hit is False
        second = extract_attachment(_ref(attachment_id="att-002"), provider=provider)
        assert second.cache_hit is True
        # Cached fields are byte-identical to first call's fields.
        assert [f.model_dump() for f in second.fields] == [
            f.model_dump() for f in first.fields
        ]
        # attachment_id is updated to the new caller's ref.
        assert second.attachment_id == "att-002"

    def test_different_tenant_same_fingerprint_misses_cache(self, provider):
        """ADR-038 §5.8 — tenant-isolated cache. Same template
        fingerprint across tenants must NOT serve cross-tenant hits."""
        first = extract_attachment(_ref(tenant_id="t1"), provider=provider)
        assert first.cache_hit is False

        second = extract_attachment(_ref(tenant_id="t2"), provider=provider)
        # Tenant t2 starts cold even though fingerprint is identical.
        assert second.cache_hit is False

        # And subsequent tenant-t2 call DOES hit cache.
        third = extract_attachment(_ref(tenant_id="t2"), provider=provider)
        assert third.cache_hit is True

    def test_different_fingerprint_same_tenant_misses(self, provider):
        provider.register_fixture(
            fingerprint_for_template("walmart_po_v1"),
            [ExtractedField(name="po_number", value="WMT-1", confidence=0.99)],
        )
        first = extract_attachment(_ref(template_id="kroger_po_v3"), provider=provider)
        second = extract_attachment(_ref(template_id="walmart_po_v1"), provider=provider)
        assert first.cache_hit is False
        assert second.cache_hit is False

    def test_provider_called_only_once_per_unique_fingerprint(self):
        # Track call count manually.
        class CountingProvider(StubMultimodalProvider):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def extract(self, attachment, fields_hint=None):
                self.calls += 1
                return super().extract(attachment, fields_hint)

        prov = CountingProvider()
        prov.register_fixture(
            fingerprint_for_template("kroger_po_v3"),
            [ExtractedField(name="po_number", value="KRO-001", confidence=0.99)],
        )
        for _ in range(5):
            extract_attachment(_ref(), provider=prov)
        # 5 calls; only 1 hit the provider (cache served the rest).
        assert prov.calls == 1


# ---------------------------------------------------------------------------
# attachment_ref_from_metadata helper
# ---------------------------------------------------------------------------


class TestAttachmentRefFromMetadata:
    def test_full_metadata_round_trips(self):
        ref = attachment_ref_from_metadata(
            tenant_id="t1",
            case_id="case-1",
            raw={
                "attachment_id": "att-A",
                "name": "po.pdf",
                "mime_type": "application/pdf",
                "bytes": 4096,
                "template_fingerprint": "abcd",
            },
        )
        assert ref.attachment_id == "att-A"
        assert ref.tenant_id == "t1"
        assert ref.case_id == "case-1"
        assert ref.bytes == 4096
        assert ref.template_fingerprint == "abcd"

    def test_missing_fingerprint_derived_from_name(self):
        ref = attachment_ref_from_metadata(
            tenant_id="t1", case_id=None,
            raw={"name": "po.pdf", "mime_type": "application/pdf", "bytes": 1},
        )
        # Stable fingerprint derived from name.
        assert ref.template_fingerprint == fingerprint_for_template("po.pdf")

    def test_missing_optional_fields_defaults(self):
        ref = attachment_ref_from_metadata(
            tenant_id="t1", case_id=None,
            raw={"name": "x", "mime_type": "image/png"},
        )
        assert ref.bytes == 0  # missing → 0
        assert ref.case_id is None
