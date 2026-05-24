"""ADR-042 Phase 2 — SAP Data section schema contract.

Backend contract for the Customer Inbox SAP Data tab. Composer adapter + UI
projector land in follow-up increments; this locks the typed shape and that
AnalysisResponse carries it (preview-only, Guardrail #7).
"""

from __future__ import annotations

from api.schemas import AnalysisResponse, SapDataAnalysisData


def test_sap_data_holds_system_state_and_optional_value() -> None:
    d = SapDataAnalysisData(
        system="S4H_PRD",
        validation_status="SO confirmed, ATP OK",
        order_value_usd=45200.0,
        sap_doc_number="5100012344",
    )
    assert d.system == "S4H_PRD"
    assert d.order_value_usd == 45200.0


def test_sap_data_optional_fields_default_none() -> None:
    d = SapDataAnalysisData(system="ECC_PRD", validation_status="On hold")
    assert d.order_value_usd is None
    assert d.sap_doc_number is None


def test_analysis_response_carries_sap_data_section() -> None:
    ar = AnalysisResponse(
        diagnosis="d", confidence=90, risk="low", resolution="r",
        sap_data_analysis=SapDataAnalysisData(
            system="S4H_PRD", validation_status="SO confirmed",
        ),
    )
    assert ar.sap_data_analysis is not None
    assert ar.sap_data_analysis.system == "S4H_PRD"
    # Absent by default — UI suppresses the tab when None (data-presence).
    assert AnalysisResponse(
        diagnosis="d", confidence=90, risk="low", resolution="r",
    ).sap_data_analysis is None
