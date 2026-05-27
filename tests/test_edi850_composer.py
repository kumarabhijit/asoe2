"""ADR-042 Phase 5 — Edi850Document composer + AnalysisResponse wiring.

Composer projects the deterministically built EDI 850 from enrichment_context;
None until the `edi_850` builder producer lands (preview-only, Guardrail #6).
Construction lives in the builder (tests/test_edi850_builder.py); here we lock
the projection + None-on-absent/malformed + the AnalysisResponse field.
"""

from __future__ import annotations

from api.profile_composer import compose_edi_850_document
from api.schemas import AnalysisResponse, Edi850Document
from api.store import ChildCase
from gateways.edi850 import build_edi_850


def _record(**overrides) -> ChildCase:
    base = dict(
        tenant_id="acme-corp", order_id="SO-1", event_type="MANUAL_ORDER_INTAKE",
        trace_id="tr-1", intent="MANUAL_ORDER_INTAKE",
        lifecycle_state="PENDING_REVIEW", shadow_verdict="YELLOW",
        resolution_data={},
    )
    base.update(overrides)
    return ChildCase(**base)


_CTX = build_edi_850(
    order_id="SO-1", po_number="0093847612", po_date="2025-03-17",
    buyer_name="Walmart Stores Inc", buyer_id="300001",
    seller_name="Acme Beverages Co", seller_id="VENDOR-7788",
    line_items=[{"line_num": "001", "material": "BEV-COLA-12PK",
                 "quantity": 480, "uom": "CS", "unit_price": 8.64}],
)


def test_compose_none_when_context_absent() -> None:
    assert compose_edi_850_document(_record()) is None


def test_compose_projects_from_context() -> None:
    out = compose_edi_850_document(
        _record(enrichment_context={"edi_850": _CTX})
    )
    assert out is not None
    assert out.transaction_set == "850"
    assert out.header.po_number == "0093847612"
    assert out.line_items[0].product_id == "BEV-COLA-12PK"
    assert out.raw_x12.startswith("ISA*")
    assert any(s.seg_id == "PO1" for s in out.segments)


def test_compose_none_on_malformed() -> None:
    bad = {"standard": "ANSI X12 5010"}  # missing required envelope/header/totals
    assert compose_edi_850_document(
        _record(enrichment_context={"edi_850": bad})
    ) is None


def test_analysis_response_carries_edi_850_audit() -> None:
    ar = AnalysisResponse(
        diagnosis="d", confidence=90, risk="low", resolution="r",
        edi_850_audit=Edi850Document(**_CTX),
    )
    assert ar.edi_850_audit is not None
    assert AnalysisResponse(
        diagnosis="d", confidence=90, risk="low", resolution="r",
    ).edi_850_audit is None
