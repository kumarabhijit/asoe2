"""Locks the section → presentation_tier authority (council 2026-06-07).

These assignments drive where the UI shows each section (Layer 1 /
Evidence / Diagnostics drawer). The UI honors the map; this test is the
backend-side guard that the placement stays deterministic.
"""

from api.presentation_tiers import DEFAULT_TIER, SECTION_TIERS, section_tiers, tier_of


def test_unknown_section_defaults_to_evidence():
    # Fail-open toward operator visibility — a new section is shown
    # (collapsed, Layer 2), never buried in the audit drawer by default.
    assert tier_of("some_new_intent_section") == "evidence"
    assert DEFAULT_TIER == "evidence"


def test_engine_artifacts_are_audit():
    # X12 reconstruction + derived entity graph are engine internals.
    assert tier_of("edi_850_audit") == "audit"
    assert tier_of("knowledge_graph") == "audit"


def test_justification_sections_are_evidence():
    for key in (
        "price_analysis",
        "duplicate_detection",
        "order_comparison",
        "email_source",
        "sap_data_analysis",
        "agent_analysis",
    ):
        assert tier_of(key) == "evidence", key


def test_no_section_is_operator_by_default():
    # Layer 1 is Situation + Recommendation only; no enrichment section
    # promotes itself onto the operator's primary surface.
    assert "operator" not in SECTION_TIERS.values()


def test_section_tiers_returns_a_copy():
    snapshot = section_tiers()
    snapshot["price_analysis"] = "audit"
    assert SECTION_TIERS["price_analysis"] == "evidence"
