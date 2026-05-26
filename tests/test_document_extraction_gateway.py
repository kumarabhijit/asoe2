"""ADR-045 P2.6 — DocumentExtractionGateway (candidate proposer → verified anchors).

Red-green never hits a live model: the default backend is the
RecordedDocumentExtractionBackend replaying a frozen candidate set +
field-selections fixture. The gateway SELECTS candidate boxes (never generates
geometry), runs the runtime verifier, and is idempotent keyed on
(sha256, model_id). Circuit-breaker parity: an OPEN breaker degrades to no
geometry (the composer falls back to ADR-043 text anchors).
"""

from __future__ import annotations

import pytest

from gateways import circuit_breaker
from gateways.document_extraction import (
    GATEWAY_NAME,
    DocumentExtractionGateway,
    RecordedDocumentExtractionBackend,
)

_SHA = "f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1"


@pytest.fixture(autouse=True)
def _reset_breaker():
    circuit_breaker.reset_all()
    yield
    circuit_breaker.reset_all()


def _gateway() -> DocumentExtractionGateway:
    return DocumentExtractionGateway(backend=RecordedDocumentExtractionBackend())


def test_replays_candidate_set_into_verified_spatial_anchors():
    gw = _gateway()
    anchors = gw.extract_anchors(
        attachment_id="att-1", source_sha256=_SHA, hint={"case": "PO_8842"},
    )
    assert len(anchors) == 4
    po = next(a for a in anchors if a.supports_ref == "order_entry.customer_po")
    assert po.anchor_source == "spatial_extracted"
    assert po.page == 1 and po.bbox == [0.08, 0.12, 0.52, 0.16]
    assert po.text == "PO# EML-PO-2026-0042"
    assert po.label == "PO number"
    assert po.confidence == 0.98


def test_replay_is_idempotent_and_byte_identical_on_sha_model_key():
    gw = _gateway()
    first = gw.extract_anchors(attachment_id="att-1", source_sha256=_SHA, hint={"case": "PO_8842"})
    second = gw.extract_anchors(attachment_id="att-1", source_sha256=_SHA, hint={"case": "PO_8842"})
    assert [a.model_dump() for a in first] == [a.model_dump() for a in second]


def test_mismatched_candidate_text_degrades_to_text_anchor():
    # If the OCR candidate text under the selected box doesn't equal the field
    # text, the verifier drops geometry (degrade-to-text; geometry not required
    # for audit) rather than draw a confident box over unverified content.
    backend = RecordedDocumentExtractionBackend()
    # Poison one candidate's text so verification fails for that field.
    backend._by_case["PO_8842"]["candidates"][0]["text"] = "WRONG OCR TOKEN"
    gw = DocumentExtractionGateway(backend=backend)
    anchors = gw.extract_anchors(attachment_id="att-1", source_sha256=_SHA, hint={"case": "PO_8842"})
    po = next(a for a in anchors if a.supports_ref == "order_entry.customer_po")
    assert po.anchor_source == "text_derived"
    assert po.page is None and po.bbox is None


def test_binds_rendition_hash_when_supplied():
    gw = _gateway()
    anchors = gw.extract_anchors(
        attachment_id="att-1", source_sha256=_SHA, hint={"case": "PO_8842"},
        rendition_hash="rh-123",
    )
    assert all(a.rendition_hash == "rh-123" for a in anchors)


def test_open_breaker_degrades_to_no_geometry():
    # Force the gateway breaker OPEN; extraction returns [] so the composer falls
    # back to ADR-043 text anchors — an OCR outage never blocks the operator view.
    breaker = circuit_breaker.get_breaker(GATEWAY_NAME)
    for _ in range(50):
        breaker.record_failure(10.0)
    gw = _gateway()
    anchors = gw.extract_anchors(attachment_id="att-1", source_sha256=_SHA, hint={"case": "PO_8842"})
    assert anchors == []


def test_missing_recording_fails_loud():
    gw = _gateway()
    with pytest.raises(KeyError):
        gw.extract_anchors(attachment_id="att-1", source_sha256=_SHA, hint={"case": "UNKNOWN"})
