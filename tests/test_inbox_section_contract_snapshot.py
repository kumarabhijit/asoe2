"""Phase 8 hardening — field-level contract snapshot for the ADR-042 sections.

`test_openapi_contract.py` catches *any* schema drift (coarse: "regenerate"),
and `test_inbox_gate_openapi_contract.py` asserts the 8 section schema *names*
exist. Neither pins the per-section FIELD SET — yet these `*AnalysisData`-style
sections are the SOX-relevant evidence contract (CLAUDE.md Guardrail #6/#7), so
adding / removing / renaming a field must be a deliberate, reviewable change.

This snapshot locks each section model's exact field names. A field move fails
here with a precise diff; the fix is to update the snapshot in the SAME PR (which
makes the contract change visible to the compliance CODEOWNERS), not to silently
regenerate. The asoe-ui `src/types/exceptions.ts` mirror must move in lockstep.
"""

from __future__ import annotations

import pytest

from api import schemas

# The frozen contract surface — section model → exact field-name set.
# Update DELIBERATELY (with the matching asoe-ui type mirror) when the evidence
# contract changes; the diff is the audit record of that change.
_SECTION_FIELDS: dict[str, set[str]] = {
    "OrderEntryExtraction": {
        "source_type", "confidence", "confidence_signal", "header",
        "customer_name", "customer_bp", "line_items", "validation_flags",
    },
    "Edi850Document": {
        "standard", "transaction_set", "envelope", "header", "parties",
        "line_items", "totals", "segments", "raw_x12",
    },
    "ConstraintCheck": {
        "name", "status", "detail", "metric", "agent", "system_ref",
    },
    "ConstraintEvaluation": {
        "lifecycle_stages", "lifecycle_index", "change_items", "checks",
        "pass_count", "conditional_count", "warning_count",
    },
    "ScenarioOption": {
        "name", "description", "recommended", "impact", "financial_delta_usd",
    },
    "ChangeDecision": {
        "recommended_action", "confidence", "rationale", "revenue_impact_usd",
        "requires_cosign", "sap_actions",
    },
    "KnowledgeGraphPayload": {"nodes", "edges", "root_id"},
    "DraftReply": {
        "status", "reason", "template_name", "recipient", "subject", "body",
        "edits_applied", "drafted_by", "drafted_at", "revisions",
    },
    "DraftReplyRevision": {
        "version", "subject", "body", "edits_applied", "author",
        "authored_at", "source",
    },
}


@pytest.mark.parametrize("model_name, expected", sorted(_SECTION_FIELDS.items()))
def test_section_field_contract_is_frozen(model_name: str, expected: set[str]) -> None:
    model = getattr(schemas, model_name)
    actual = set(model.model_fields)
    assert actual == expected, (
        f"{model_name} field contract drifted.\n"
        f"  added:   {sorted(actual - expected)}\n"
        f"  removed: {sorted(expected - actual)}\n"
        "If intentional: update _SECTION_FIELDS here AND the asoe-ui "
        "src/types/exceptions.ts mirror in the same PR (compliance CODEOWNERS "
        "review the evidence-contract change)."
    )


def test_analysis_response_carries_every_section() -> None:
    # The composer's output contract must expose every section field, so a
    # section can't be built but left unwired from AnalysisResponse.
    fields = set(schemas.AnalysisResponse.model_fields)
    for section_field in (
        "order_entry_extraction", "edi_850_audit", "change_analysis",
        "knowledge_graph", "draft_reply", "entities_analysis",
        "sap_data_analysis",
    ):
        assert section_field in fields, f"AnalysisResponse missing {section_field}"
