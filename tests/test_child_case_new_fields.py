"""Phase 2 step 2 — ExceptionRecord (child case) carries the new fields
and surfaces them on the API response shapes.

Covers the additive changes to:
  - api/store.py:ExceptionRecord (5 new optional attributes)
  - api/store.py:ExceptionRecord.to_summary / to_detail (pass-through)
  - api/schemas.py:ExceptionSummary / ExceptionDetailResponse (Pydantic
    surface).
"""

from __future__ import annotations

from api.store import ExceptionRecord


def _make() -> ExceptionRecord:
    return ExceptionRecord(
        tenant_id="t1", order_id="ord1", event_type="TEST", trace_id="tr1",
        supergroup_code="SG_BLOCK_PRICING",
        intent_code="INT_PRICE_MISMATCH",
        divergence_reason=None,
        sap_block_field="LIFSK",
        scope="ITEM",
    )


def test_exception_record_accepts_new_fields():
    rec = _make()
    assert rec.supergroup_code == "SG_BLOCK_PRICING"
    assert rec.intent_code == "INT_PRICE_MISMATCH"
    assert rec.divergence_reason is None
    assert rec.sap_block_field == "LIFSK"
    assert rec.scope == "ITEM"


def test_new_fields_default_to_none():
    rec = ExceptionRecord(
        tenant_id="t1", order_id="ord1", event_type="TEST", trace_id="tr1",
    )
    assert rec.supergroup_code is None
    assert rec.intent_code is None
    assert rec.divergence_reason is None
    assert rec.sap_block_field is None
    assert rec.scope is None


def test_summary_surfaces_supergroup_and_intent():
    rec = _make()
    summary = rec.to_summary()
    assert summary.supergroup_code == "SG_BLOCK_PRICING"
    assert summary.intent_code == "INT_PRICE_MISMATCH"


def test_detail_surfaces_all_new_fields():
    rec = _make()
    detail = rec.to_detail()
    assert detail.supergroup_code == "SG_BLOCK_PRICING"
    assert detail.intent_code == "INT_PRICE_MISMATCH"
    assert detail.divergence_reason is None
    assert detail.sap_block_field == "LIFSK"
    assert detail.scope == "ITEM"


def test_summary_omits_new_fields_when_unset():
    """Legacy records (pre-Phase 2 classifier wiring) serialise the new
    fields as None — not missing, not raising."""
    rec = ExceptionRecord(
        tenant_id="t1", order_id="ord1", event_type="TEST", trace_id="tr1",
    )
    summary = rec.to_summary()
    assert summary.supergroup_code is None
    assert summary.intent_code is None
