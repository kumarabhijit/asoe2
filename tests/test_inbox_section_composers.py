"""ADR-042 Phase 2 — composer wiring for the Entities / SAP Data sections.

Deterministic (no LLM): the composers project the sections from
`enrichment_context`, returning None when the backing gateway context is
absent (preview-only; Guardrail #6 no partial-truth). This is the seam that
auto-activates the tabs once the intake-extraction / SAP-read gateways land.
"""

from __future__ import annotations

from api.profile_composer import (
    compose_entities_analysis,
    compose_sap_data_analysis,
)
from api.store import ChildCase


def _record(**overrides) -> ChildCase:
    base = dict(
        tenant_id="acme-corp",
        order_id="SO-TEST-001",
        event_type="MANUAL_ORDER_INTAKE",
        trace_id="tr-1",
        intent="MANUAL_ORDER_INTAKE",
        lifecycle_state="PENDING_REVIEW",
        shadow_verdict="YELLOW",
        resolution_data={},
    )
    base.update(overrides)
    return ChildCase(**base)


# ── Entities ──────────────────────────────────────────────────────────────

def test_entities_none_when_context_absent() -> None:
    assert compose_entities_analysis(_record()) is None
    assert compose_entities_analysis(
        _record(enrichment_context={"inbox_entities": {"extracted": []}})
    ) is None


def test_entities_projected_from_context() -> None:
    rec = _record(
        enrichment_context={
            "inbox_entities": {
                "extracted": [
                    {"key": "primary", "value": "4500023421", "kind": "order_id",
                     "confidence": 0.97, "source_span": "order 4500023421"},
                    {"key": "po", "value": "0093847612", "kind": "po"},
                ]
            }
        }
    )
    out = compose_entities_analysis(rec)
    assert out is not None
    assert [e.value for e in out.extracted] == ["4500023421", "0093847612"]
    assert out.extracted[1].confidence is None  # optional, absent


def test_entities_none_on_malformed_rows() -> None:
    rec = _record(enrichment_context={"inbox_entities": {"extracted": [{"bad": 1}]}})
    assert compose_entities_analysis(rec) is None


def test_entities_evidence_ref_passes_through_when_producer_emits_it() -> None:
    # ADR-043 field↔source linking: evidence_ref is a pass-through from the
    # extraction producer (it stamps the same ref it put on the matching
    # EvidenceAnchor.supports_ref). The composer must carry it verbatim — it
    # does NOT synthesise the pairing (Guardrail #6, backend-authoritative).
    rec = _record(
        enrichment_context={
            "inbox_entities": {
                "extracted": [
                    {"key": "primary", "value": "4500023421", "kind": "order_id",
                     "source_span": "order 4500023421",
                     "evidence_ref": "order_entry.primary"},
                    {"key": "po", "value": "0093847612", "kind": "po"},
                ]
            }
        }
    )
    out = compose_entities_analysis(rec)
    assert out is not None
    assert out.extracted[0].evidence_ref == "order_entry.primary"
    # Absent in the source row → None (optional provenance, no fabrication).
    assert out.extracted[1].evidence_ref is None


def test_entities_confidence_signal_projected_from_scalar() -> None:
    # ADR-032 — per-entity calibration signal projected from the scalar
    # confidence; uncalibrated until the loop ships. Entities without a
    # confidence stay signal-free (no fabrication).
    rec = _record(
        enrichment_context={
            "inbox_entities": {
                "extracted": [
                    {"key": "primary", "value": "X", "kind": "order_id", "confidence": 0.97},
                    {"key": "po", "value": "Y", "kind": "po"},  # no confidence
                ]
            }
        }
    )
    out = compose_entities_analysis(rec)
    assert out is not None
    sig = out.extracted[0].confidence_signal
    assert sig is not None and sig.value == 0.97 and sig.calibrated is False
    assert out.extracted[1].confidence_signal is None


# ── SAP Data ────────────────────────────────────────────────────────────────

def test_sap_none_when_context_absent_or_incomplete() -> None:
    assert compose_sap_data_analysis(_record()) is None
    # Missing validation_status → None (audit-bearing anchor absent).
    assert compose_sap_data_analysis(
        _record(enrichment_context={"sap_data": {"system": "S4H_PRD"}})
    ) is None


def test_sap_projected_from_context() -> None:
    rec = _record(
        enrichment_context={
            "sap_data": {
                "system": "S4H_PRD",
                "validation_status": "SO confirmed, ATP OK",
                "order_value_usd": 45200.0,
                "sap_doc_number": "5100012344",
            }
        }
    )
    out = compose_sap_data_analysis(rec)
    assert out is not None
    assert out.system == "S4H_PRD"
    assert out.order_value_usd == 45200.0
