"""ADR-034 Phase G adapter tests — EmailOrderEntry → email_source secondary.

Covers `adapt_email_source` projecting
`record.enrichment_context["email_source_context"]` (the
`email_intake/fetch_message` gateway response) into `EmailSourceData`.

Cases:
  * Happy path: full gateway response → typed projection with manifest.
  * Empty / missing context → adapter returns None (composer treats as
    structural omission for the secondary; the section simply doesn't
    mount).
  * Malformed attachment entries are silently dropped rather than
    poisoning the manifest list.
  * body_excerpt is contextual — None / absent is allowed.
"""

from __future__ import annotations

from api.analysis_adapters import adapt_email_source
from api.store import ChildCase


def _record(*, enrichment_context=None) -> ChildCase:
    return ChildCase(
        tenant_id="t1",
        order_id="EML-PO-2026-0042",
        event_type="EMAIL_ORDER_ENTRY_REQUEST",
        trace_id="trace-eoe-g-1",
        intent="MANUAL_ORDER_INTAKE",
        selected_recipe="EmailOrderEntryRecipe.py",
        resolution_data={},
        original_event=None,
        enrichment_context=enrichment_context or {},
    )


def _full_payload() -> dict:
    return {
        "from_address": "buyer@stub-customer.example",
        "received_at": "2026-04-30T10:12:00Z",
        "subject": "PO submission — stub fixture",
        "body_hash": "0" * 64,
        "attachment_manifest": [
            {
                "name": "purchase_order.pdf", "mime_type": "application/pdf",
                "bytes": 12_345, "sha256": "a" * 64, "attachment_id": "att-1",
            },
            {"name": "ship_to.csv", "mime_type": "text/csv", "bytes": 412},
        ],
        "body_excerpt": "Please process the attached PO. Ship to the Atlanta DC.",
    }


class TestAdaptEmailSourceHappyPath:
    def test_full_payload_projects_into_typed_model(self):
        record = _record(
            enrichment_context={"email_source_context": _full_payload()},
        )
        result = adapt_email_source(record)
        assert result is not None
        assert result.from_address == "buyer@stub-customer.example"
        assert result.received_at == "2026-04-30T10:12:00Z"
        assert result.subject == "PO submission — stub fixture"
        assert result.body_hash == "0" * 64
        assert len(result.attachment_manifest) == 2
        assert result.attachment_manifest[0].name == "purchase_order.pdf"
        assert result.attachment_manifest[0].mime_type == "application/pdf"
        # sha256 (tamper-evidence) + attachment_id (download ref) pass through.
        assert result.attachment_manifest[0].sha256 == "a" * 64
        assert result.attachment_manifest[0].attachment_id == "att-1"
        # Absent on the second entry → None (preview-only until stored).
        assert result.attachment_manifest[1].sha256 is None
        assert result.attachment_manifest[1].attachment_id is None
        assert result.attachment_manifest[0].bytes == 12_345
        assert result.body_excerpt == (
            "Please process the attached PO. Ship to the Atlanta DC."
        )

    def test_body_excerpt_optional(self):
        payload = _full_payload()
        del payload["body_excerpt"]
        record = _record(enrichment_context={"email_source_context": payload})
        result = adapt_email_source(record)
        assert result is not None
        assert result.body_excerpt is None


class TestAdaptEmailSourceAbsence:
    def test_missing_context_returns_none(self):
        # No email_source_context at all — the gateway didn't run or
        # this isn't an email-channel record. Secondary returns None;
        # composer treats as structural omission (section doesn't mount).
        record = _record(enrichment_context={})
        assert adapt_email_source(record) is None

    def test_empty_context_returns_none(self):
        record = _record(enrichment_context={"email_source_context": {}})
        assert adapt_email_source(record) is None

    def test_non_dict_context_returns_none(self):
        # Defensive — a malformed gateway response payload. Adapter must
        # not crash on a list / string / other shape.
        record = _record(enrichment_context={"email_source_context": ["unexpected"]})
        assert adapt_email_source(record) is None


class TestAttachmentManifestRobustness:
    def test_malformed_entries_are_dropped_not_poisoning_the_list(self):
        payload = _full_payload()
        payload["attachment_manifest"] = [
            "not-a-dict",  # silently dropped
            {"name": "valid.pdf", "mime_type": "application/pdf", "bytes": 100},
            {"name": "size_not_int", "mime_type": "text/csv", "bytes": "many"},
            None,  # silently dropped
        ]
        record = _record(enrichment_context={"email_source_context": payload})
        result = adapt_email_source(record)
        assert result is not None
        # Two entries survive: the valid one, and the size-not-int entry
        # whose `bytes` coerces via int(...) — Python's `int("many")`
        # raises ValueError so the catch-and-drop path applies.
        assert len(result.attachment_manifest) == 1
        assert result.attachment_manifest[0].name == "valid.pdf"

    def test_non_list_manifest_treated_as_empty(self):
        payload = _full_payload()
        payload["attachment_manifest"] = "not-a-list"
        record = _record(enrichment_context={"email_source_context": payload})
        result = adapt_email_source(record)
        assert result is not None
        assert result.attachment_manifest == []
