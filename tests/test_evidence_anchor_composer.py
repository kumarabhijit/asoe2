"""CP-B RED gate (ADR-043 §2.2, §2.5) — composer projects EvidenceAnchors.

Test-first (`xfail(strict=True)`; removed at CP-C). Asserts the backend is the
sole assembler of highlight anchors (Guardrail #6): from a frozen
`enrichment_context`, `build_evidence_anchors` derives one `text_derived` anchor
per extracted evidence field, each bound to the attachment `sha256`, labelled,
and carrying NO geometry. The UI later only *locates* these — it invents nothing.
"""

from __future__ import annotations

from api.store import ChildCase


def _record() -> ChildCase:
    return ChildCase(
        tenant_id="t1",
        order_id="EML-PO-2026-0042",
        event_type="EMAIL_ORDER_ENTRY_REQUEST",
        trace_id="trace-anchor-1",
        intent="MANUAL_ORDER_INTAKE",
        selected_recipe="EmailOrderEntryRecipe.py",
        resolution_data={},
        original_event=None,
        enrichment_context={
            "email_source_context": {
                "from_address": "buyer@stub-customer.example",
                "received_at": "2026-04-30T10:12:00Z",
                "subject": "PO submission",
                "body_hash": "0" * 64,
                "attachment_manifest": [
                    {
                        "name": "purchase_order.pdf",
                        "mime_type": "application/pdf",
                        "bytes": 12_345,
                        "sha256": "a" * 64,
                        "attachment_id": "att-1",
                    },
                ],
            },
            # Extracted evidence with verbatim source_span (the only provenance
            # we have today; Phase-1 anchors derive entirely from this).
            "extracted_entities": [
                {"key": "po_number", "value": "PO-2026-0042", "kind": "po",
                 "source_span": "PO-2026-0042"},
                {"key": "ship_to", "value": "Atlanta DC", "kind": "address",
                 "source_span": "Ship to: Atlanta DC"},
                {"key": "qty", "value": "500", "kind": "qty",
                 "source_span": "Quantity 500"},
            ],
        },
    )


def test_projects_an_anchor_for_every_extracted_field():
    from api.analysis_adapters import build_evidence_anchors

    anchors = build_evidence_anchors(_record())
    assert len(anchors) >= 3  # all fields highlighted by default (PO decision 1)


def test_all_anchors_are_text_derived_with_no_geometry():
    from api.analysis_adapters import build_evidence_anchors

    for a in build_evidence_anchors(_record()):
        assert a.anchor_source == "text_derived"
        assert a.page is None and a.bbox is None and a.confidence is None


def test_anchors_are_bound_to_the_attachment_bytes():
    from api.analysis_adapters import build_evidence_anchors

    for a in build_evidence_anchors(_record()):
        assert a.attachment_id == "att-1"
        assert a.source_sha256 == "a" * 64  # binds to exact bytes (D2/D8)


def test_each_anchor_is_labelled_from_a_closed_vocabulary():
    from api.analysis_adapters import build_evidence_anchors

    anchors = build_evidence_anchors(_record())
    assert all(a.label for a in anchors)
    assert all(a.supports_kind in {"extracted_field", "constraint", "decision"}
               for a in anchors)
    assert any(a.text == "PO-2026-0042" for a in anchors)


def test_no_anchors_without_a_stored_attachment():
    # No sha256/attachment_id on the manifest → nothing to overlay → no
    # anchors (never a highlight pointing at bytes we don't hold).
    from api.analysis_adapters import build_evidence_anchors

    rec = _record()
    rec.enrichment_context["email_source_context"]["attachment_manifest"] = [
        {"name": "x.pdf", "mime_type": "application/pdf", "bytes": 1},
    ]
    assert build_evidence_anchors(rec) == []


# ── ADR-045 P2.8 — composer prefers a verified spatial anchor, degrades to text ──

def _spatial_anchor_dict(*, supports_ref, sha="a" * 64, attachment_id="att-1"):
    return {
        "attachment_id": attachment_id,
        "anchor_source": "spatial_extracted",
        "text": "PO-2026-0042",
        "match_key": {"normalized_text": "po-2026-0042", "occurrence_index": 0},
        "supports_kind": "extracted_field",
        "supports_ref": supports_ref,
        "label": "PO number",
        "source_sha256": sha,
        "page": 1,
        "bbox": [0.1, 0.1, 0.4, 0.2],
        "confidence": 0.97,
        "rendition_hash": "rh-1",
    }


def test_spatial_anchor_overrides_text_when_available_and_bound():
    from api.analysis_adapters import build_evidence_anchors

    rec = _record()
    rec.enrichment_context["spatial_anchors"] = [
        _spatial_anchor_dict(supports_ref="order_entry.po_number"),
    ]
    anchors = build_evidence_anchors(rec)
    po = next(a for a in anchors if a.supports_ref == "order_entry.po_number")
    assert po.anchor_source == "spatial_extracted"
    assert po.bbox == [0.1, 0.1, 0.4, 0.2] and po.page == 1
    assert po.rendition_hash == "rh-1"
    # Other fields with no spatial anchor stay text-derived (graceful mix).
    ship = next(a for a in anchors if a.supports_ref == "order_entry.ship_to")
    assert ship.anchor_source == "text_derived"


def test_spatial_anchor_ignored_when_bound_to_different_bytes():
    # A spatial anchor whose source_sha256 doesn't match the stored attachment
    # must never re-point a highlight — degrade to the text anchor.
    from api.analysis_adapters import build_evidence_anchors

    rec = _record()
    rec.enrichment_context["spatial_anchors"] = [
        _spatial_anchor_dict(supports_ref="order_entry.po_number", sha="b" * 64),
    ]
    po = next(
        a for a in build_evidence_anchors(rec)
        if a.supports_ref == "order_entry.po_number"
    )
    assert po.anchor_source == "text_derived"
    assert po.bbox is None


def test_missing_spatial_anchors_degrades_to_all_text():
    from api.analysis_adapters import build_evidence_anchors

    rec = _record()  # no spatial_anchors key at all (outage / not extracted yet)
    assert all(a.anchor_source == "text_derived" for a in build_evidence_anchors(rec))
