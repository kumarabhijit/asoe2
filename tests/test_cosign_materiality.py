"""ADR-042 DoR #3 — four-eyes cosign threshold from SAP re-price, not LLM value.

The financial materiality that gates the four-eyes cosign (ADR-040,
HIGH_VALUE_OVERRIDE_THRESHOLD_USD) must be computed from the SAP
system-of-record order value (master-data re-price, surfaced by the sap_order
producer in enrichment_context["sap_data"]), NOT the LLM-extracted
financial_impact_usd. Otherwise an upstream adversary who drives the extracted
impact below the threshold dodges cosign on a genuinely high-value order.

Written test-first; RED until `_cosign_materiality_usd` exists.
"""

from __future__ import annotations

from api.routes.exceptions import _cosign_materiality_usd
from api.store import ExceptionRecord


def _rec(**overrides) -> ExceptionRecord:
    base = dict(
        tenant_id="t", order_id="o", event_type="MANUAL_ORDER_INTAKE",
        trace_id="x", intent="MANUAL_ORDER_INTAKE", lifecycle_state="RESOLVED",
        shadow_verdict="GREEN", resolution_data={},
    )
    base.update(overrides)
    return ExceptionRecord(**base)


def test_prefers_sap_reprice_over_llm_impact() -> None:
    # LLM says $500 (a dodge); SAP system-of-record says $45,200.
    rec = _rec(
        resolution_data={"financial_impact_usd": 500.0},
        enrichment_context={"sap_data": {
            "system": "S4H_PRD", "validation_status": "SO confirmed",
            "order_value_usd": 45200.0,
        }},
    )
    assert _cosign_materiality_usd(rec) == 45200.0


def test_falls_back_to_llm_impact_when_no_sap_read() -> None:
    rec = _rec(resolution_data={"financial_impact_usd": 12000.0})
    assert _cosign_materiality_usd(rec) == 12000.0


def test_sap_present_but_no_order_value_falls_back() -> None:
    rec = _rec(
        resolution_data={"financial_impact_usd": 12000.0},
        enrichment_context={"sap_data": {
            "system": "S4H_PRD", "validation_status": "SO confirmed",
        }},
    )
    assert _cosign_materiality_usd(rec) == 12000.0


def test_none_when_neither_present() -> None:
    assert _cosign_materiality_usd(_rec()) is None
