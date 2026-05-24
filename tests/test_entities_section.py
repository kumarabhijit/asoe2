"""ADR-042 Phase 2 — Entities section schema contract.

Backend contract for the Customer Inbox Entities tab. The composer adapter +
UI projector land in follow-up increments; this locks the typed shape and that
AnalysisResponse carries it (preview-only, Guardrail #7).
"""

from __future__ import annotations

from api.schemas import (
    AnalysisResponse,
    EntitiesAnalysisData,
    ExtractedEntity,
)


def test_extracted_entity_holds_value_kind_and_optional_provenance() -> None:
    e = ExtractedEntity(
        key="order_id",
        value="4500023421",
        kind="order_id",
        confidence=0.97,
        source_span="change to order 4500023421",
    )
    assert e.kind == "order_id"
    assert e.confidence == 0.97


def test_entities_section_defaults_to_empty_list() -> None:
    assert EntitiesAnalysisData().extracted == []


def test_analysis_response_carries_entities_section() -> None:
    section = EntitiesAnalysisData(
        extracted=[ExtractedEntity(key="po", value="0093847612", kind="po")]
    )
    ar = AnalysisResponse(
        diagnosis="d", confidence=90, risk="low", resolution="r",
        entities_analysis=section,
    )
    assert ar.entities_analysis is not None
    assert ar.entities_analysis.extracted[0].value == "0093847612"
    # Absent by default — UI suppresses the tab when None (data-presence).
    assert AnalysisResponse(
        diagnosis="d", confidence=90, risk="low", resolution="r",
    ).entities_analysis is None
