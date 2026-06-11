"""Projection-completeness lock for ChildCase → response DTOs.

Phase 4 gap closure (2026-06-11): ``ChildCase.to_summary()`` and
``to_detail()`` are hand-maintained projection methods. Adding a field
to ChildCase (or to the response models) without updating the matching
projection silently ships a DTO whose field falls back to its Pydantic
default — the API "works" but returns a partial record. Nothing caught
that drift before this lock.

Invariants pinned:
  1. Every field declared on ExceptionSummary / ExceptionDetailResponse
     is populated from the equal-named ChildCase attribute (a sentinel
     record makes a dropped kwarg show up as default-vs-sentinel).
  2. Every public ChildCase attribute is either projected onto the
     detail DTO or listed in the explicit NOT_PROJECTED set below — so
     a new ChildCase field forces a conscious decision, not an
     accidental omission.
"""

from __future__ import annotations

from api.schemas import ExceptionDetailResponse, ExceptionSummary
from api.store import ChildCase

# ChildCase attributes deliberately NOT carried on the detail DTO.
# Additions here need the same scrutiny as a schema change:
#   * original_event      — replay payload for re-analysis; surfaced via
#                           the trace / reanalysis endpoints, not the DTO.
#   * enrichment_context  — composed into OrderAnalysis by build_analysis
#                           (the sole assembler); never raw on the record.
#   * sap_block_code      — raw SAP source signal (requirements §5);
#                           audit provenance surfaced via the composer /
#                           diagnostics drawer, not the record DTO.
NOT_PROJECTED_TO_DETAIL = {
    "original_event",
    "enrichment_context",
    "sap_block_code",
}


def _sentinel_record() -> ChildCase:
    """A ChildCase with every constructor argument set to a distinctive
    non-default value, so a projection that drops a kwarg surfaces as a
    sentinel-vs-default mismatch."""
    record = ChildCase(
        tenant_id="t-sentinel",
        order_id="ORD-SENTINEL",
        event_type="EVT-SENTINEL",
        trace_id="trace-sentinel",
        intent="INTENT-SENTINEL",
        lifecycle_state="RESOLVED",
        shadow_verdict="GREEN",
        selected_recipe="RecipeSentinel",
        final_status="COMPLETE",
        resolution_data={"k": "v"},
        resolved_by="user-sentinel",
        resolved_action="ACTION-SENTINEL",
        resolution_notes="notes-sentinel",
        account_id="acct-sentinel",
        account_name="Account Sentinel",
        original_event={"event": "sentinel"},
        enrichment_context={"ctx": "sentinel"},
        parent_case_id="case-sentinel",
        sap_block_code="Z1",
        supergroup_code="SG_SENTINEL",
        intent_code="IC_SENTINEL",
        divergence_reason="divergence-sentinel",
        sap_block_field="LIFSK",
        scope="ORDER",
    )
    record.reanalysis_history.append({"attempt": 1, "reason": "sentinel"})
    return record


def test_to_detail_populates_every_response_field_from_the_record():
    record = _sentinel_record()
    detail = record.to_detail()
    for name in ExceptionDetailResponse.model_fields:
        assert getattr(detail, name) == getattr(record, name), (
            f"to_detail() does not project '{name}' — the DTO falls back "
            "to its Pydantic default and the API returns a partial record"
        )


def test_to_summary_populates_every_summary_field_from_the_record():
    record = _sentinel_record()
    summary = record.to_summary()
    for name in ExceptionSummary.model_fields:
        assert getattr(summary, name) == getattr(record, name), (
            f"to_summary() does not project '{name}'"
        )


def test_every_childcase_attribute_is_projected_or_explicitly_excluded():
    record = _sentinel_record()
    attrs = {name for name in vars(record) if not name.startswith("_")}
    detail_fields = set(ExceptionDetailResponse.model_fields)
    unaccounted = attrs - detail_fields - NOT_PROJECTED_TO_DETAIL
    assert unaccounted == set(), (
        f"ChildCase attribute(s) {sorted(unaccounted)} are neither "
        "projected by to_detail() nor listed in NOT_PROJECTED_TO_DETAIL. "
        "Either add them to the DTO + projection (and the asoe-ui type "
        "mirror) or record the exclusion here with a rationale."
    )


def test_summary_is_a_strict_subset_of_detail():
    """The summary DTO must never carry a field the detail lacks —
    list rows and the detail panel must agree on shared fields."""
    extra = set(ExceptionSummary.model_fields) - set(
        ExceptionDetailResponse.model_fields
    )
    assert extra == set()
