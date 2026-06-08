"""Section → presentation_tier authority (council 2026-06-07).

The `build_analysis` composer projects each analysis section's
presentation tier onto the `PresentationContract.section_tiers` map so
the UI can place every section deterministically:

  * ``operator`` — Layer 1 (changes what the operator does).
  * ``evidence`` — Layer 2 Evidence tier (the deltas / comparisons /
    source documents that justify the recommendation).
  * ``audit``    — the Diagnostics & Audit drawer (engine internals,
    deterministic reconstructions, provenance).

The UI HONORS this map; it never re-decides placement (asoe-ui
Guardrail #0/#1). This axis is ORTHOGONAL to the audit-bearing ``tier``
in ``compliance/audit_bearing_registry.yaml`` (which governs whether a
field must be *populated*); ``presentation_tier`` governs *where a
section is shown*.

Keys are the section field names on ``AnalysisResponse`` (plus the
synthetic ``agent_analysis`` key for the reasoning-prose section the UI
renders from ``root_cause`` / ``recommendation``).
"""

from typing import Dict, Literal

PresentationTier = Literal["operator", "evidence", "audit"]

# Engine-internal / deterministic-reconstruction / provenance sections
# live in the Diagnostics & Audit drawer. Everything that justifies the
# recommendation is Evidence (Layer 2). No enrichment section is
# `operator` by default — Layer 1 is Situation + Recommendation only.
SECTION_TIERS: Dict[str, PresentationTier] = {
    # ── Evidence: deltas / comparisons / source documents ─────────────
    "agent_analysis": "evidence",
    "price_analysis": "evidence",
    "price_hold_analysis": "evidence",
    "edi_mismatch_analysis": "evidence",
    "delivery_delay_analysis": "evidence",
    "overmax_analysis": "evidence",
    "moq_analysis": "evidence",
    "pallet_analysis": "evidence",
    "duplicate_detection": "evidence",
    "order_comparison": "evidence",
    "backorder_analysis": "evidence",
    "email_order_entry_analysis": "evidence",
    "email_source": "evidence",
    "entities_analysis": "evidence",
    "sap_data_analysis": "evidence",
    "order_entry_extraction": "evidence",
    "change_analysis": "evidence",
    "draft_reply": "evidence",
    # ── Audit: engine internals / deterministic reconstructions ───────
    # X12 850 reconstruction and the derived entity-projection graph are
    # engine artifacts, not the operator's decision evidence.
    "edi_850_audit": "audit",
    "knowledge_graph": "audit",
}

# Unknown / newly-added sections default to Evidence: surfaced to the
# operator (collapsed, Layer 2) rather than buried in the drawer. A new
# intent that adds a section is therefore shown, not hidden — fail-open
# toward operator visibility, consistent with the UI-richness commitment
# (Guardrail #7). Demotion to `audit` is an explicit, reviewed decision.
DEFAULT_TIER: PresentationTier = "evidence"


def tier_of(section_key: str) -> PresentationTier:
    """Presentation tier for a single analysis section key."""
    return SECTION_TIERS.get(section_key, DEFAULT_TIER)


def section_tiers() -> Dict[str, PresentationTier]:
    """The full static authority, emitted on every PresentationContract
    so the UI can route any rendered section deterministically."""
    return dict(SECTION_TIERS)
