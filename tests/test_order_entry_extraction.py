"""ADR-042 Phase 3 — OrderEntryExtraction contract + composer.

Backend contract for the Customer Inbox Order Entry tab (the extracted order
form). Composer projects from enrichment_context; None until the extraction
gateway lands (preview-only, Guardrail #6/#7). The UI projector lands in the
next slice.
"""

from __future__ import annotations

from api.profile_composer import compose_order_entry_extraction
from api.schemas import (
    AnalysisResponse,
    OrderEntryExtraction,
    OrderEntryHeader,
    OrderEntryLineItem,
)
from api.store import ExceptionRecord


def _record(**overrides) -> ExceptionRecord:
    base = dict(
        tenant_id="acme-corp", order_id="SO-1", event_type="MANUAL_ORDER_INTAKE",
        trace_id="tr-1", intent="MANUAL_ORDER_INTAKE",
        lifecycle_state="PENDING_REVIEW", shadow_verdict="YELLOW",
        resolution_data={},
    )
    base.update(overrides)
    return ExceptionRecord(**base)


_CTX = {
    "source_type": "PDF",
    "confidence": 0.94,
    "header": {"customer_po": "0093847612", "requested_date": "2025-03-17"},
    "customer_name": "Walmart Stores Inc",
    "customer_bp": "300001",
    "line_items": [
        {"line_num": "001", "material": "BEV-COLA-12PK", "quantity": 480,
         "uom": "CS", "unit_price": 8.64, "mdm_matched": True},
    ],
    "validation_flags": [
        {"field": "line 003", "severity": "WARNING", "message": "promo allowance"},
    ],
}


def test_schema_holds_header_lines_and_validation() -> None:
    x = OrderEntryExtraction(
        source_type="Excel", confidence=0.9, header=OrderEntryHeader(),
        line_items=[OrderEntryLineItem(line_num="1", material="M", quantity=5)],
    )
    assert x.line_items[0].quantity == 5
    assert x.line_items[0].unit_price is None


def test_compose_none_when_context_absent() -> None:
    assert compose_order_entry_extraction(_record()) is None


def test_compose_projects_from_context() -> None:
    out = compose_order_entry_extraction(
        _record(enrichment_context={"order_entry_extraction": _CTX})
    )
    assert out is not None
    assert out.source_type == "PDF"
    assert out.header.customer_po == "0093847612"
    assert out.line_items[0].material == "BEV-COLA-12PK"
    assert out.validation_flags[0].severity == "WARNING"


def test_compose_none_on_malformed() -> None:
    bad = {"source_type": "PDF"}  # missing required confidence + header
    assert compose_order_entry_extraction(
        _record(enrichment_context={"order_entry_extraction": bad})
    ) is None


def test_analysis_response_carries_order_entry_extraction() -> None:
    ar = AnalysisResponse(
        diagnosis="d", confidence=90, risk="low", resolution="r",
        order_entry_extraction=OrderEntryExtraction(
            source_type="EMAIL_BODY", confidence=0.8, header=OrderEntryHeader(),
        ),
    )
    assert ar.order_entry_extraction is not None
    assert AnalysisResponse(
        diagnosis="d", confidence=90, risk="low", resolution="r",
    ).order_entry_extraction is None
