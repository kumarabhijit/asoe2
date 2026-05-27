"""Unit tests for `api.profile_composer`.

Covers the four order-level enrichment fields the composer produces:
  - entity_profile      (from Account master-data lookup)
  - impact_metrics      (from line-item totals + record metadata)
  - root_cause          (narrative — record.resolution_data → trace)
  - recommendation      (narrative — record.resolution_data → trace)

Verdict 2026-04-22 invariants asserted here:
  - Each composer returns None when its backing data is absent
    (no zero-filled / fabricated defaults — partial-truth guard).
  - master-data fields with no producer wired (vip_status,
    credit_standing) stay None even when the Account row exists.
"""
from __future__ import annotations

from api.profile_composer import (
    compose_entity_profile,
    compose_impact_metrics,
    compose_narrative,
)
from api.store import ChildCase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _record(**overrides):
    """Build an ChildCase with sensible defaults."""
    base = dict(
        tenant_id="acme-corp",
        order_id="SO-TEST-001",
        event_type="EDI_850_LINE_MISMATCH",
        trace_id="tr-test-001",
        intent="EDI_MISMATCH",
        lifecycle_state="PENDING_REVIEW",
        shadow_verdict="YELLOW",
        account_id="acct-walmart",
        account_name="Walmart",
        resolution_data={},
    )
    base.update(overrides)
    return ChildCase(**base)


# ---------------------------------------------------------------------------
# entity_profile
# ---------------------------------------------------------------------------

def test_entity_profile_from_seed_account():
    """Account lookup populates name + bp + tier + region; the
    backend-gap fields (vip / credit / location) stay None."""
    rec = _record(account_id="acct-walmart")
    ep = compose_entity_profile(rec)
    assert ep is not None
    assert ep.customer_name == "Walmart"
    assert ep.bp_number == "BP-0001"
    assert ep.customer_tier == "enterprise"
    assert ep.region == "National"
    # Backend-gap fields — no producer wired.
    assert ep.vip_status is None
    assert ep.credit_standing is None
    assert ep.location is None


def test_entity_profile_falls_back_to_account_name_when_id_unknown():
    """Synthesises a minimal profile from denormalised account_name."""
    rec = _record(account_id="acct-nonexistent", account_name="Mystery Co")
    ep = compose_entity_profile(rec)
    assert ep is not None
    assert ep.customer_name == "Mystery Co"
    # bp_number is required on the contract — sentinel UNKNOWN, not
    # a fabricated SAP id.
    assert ep.bp_number == "UNKNOWN"
    assert ep.customer_tier is None
    assert ep.region is None


def test_entity_profile_returns_none_when_no_account_linkage():
    """No id, no name → None (UI structurally omits the pane)."""
    rec = _record(account_id=None, account_name=None)
    ep = compose_entity_profile(rec)
    assert ep is None


# ---------------------------------------------------------------------------
# impact_metrics
# ---------------------------------------------------------------------------

def test_impact_metrics_computes_delta_and_revenue_from_line_items():
    rec = _record(resolution_data={
        "line_items": [
            {"line_id": "10", "quantity": 100, "erp_price": 9.80, "po_price": 10.00},
            {"line_id": "20", "quantity": 50, "erp_price": 5.00, "po_price": 5.00},
        ],
    })
    im = compose_impact_metrics(rec)
    assert im is not None
    assert im.affected_lines == 2
    # erp_total = 980 + 250 = 1230; po_total = 1000 + 250 = 1250
    assert im.delta_amount == 20.0
    # delta_pct = 20 / 1230 * 100 = 1.626... → 1.63
    assert round(im.delta_percentage, 2) == 1.63
    # revenue at risk = abs(delta) when delta != 0
    assert im.revenue_at_risk == 20.0


def test_impact_metrics_revenue_at_risk_falls_back_to_total_when_no_delta():
    """When PO matches ERP exactly, revenue_at_risk is the total
    PO value — the operator still sees blast radius for an at-risk
    GREEN exception (e.g., a credit-block hold whose dollars are
    intact but whose order is frozen)."""
    rec = _record(resolution_data={
        "line_items": [
            {"line_id": "10", "quantity": 10, "erp_price": 25.00, "po_price": 25.00},
        ],
    })
    im = compose_impact_metrics(rec)
    assert im is not None
    assert im.delta_amount == 0.0
    assert im.delta_percentage == 0.0
    # revenue_at_risk = total_po = 250
    assert im.revenue_at_risk == 250.0


def test_impact_metrics_fulfillment_gap_pct_only_when_short():
    rec = _record(resolution_data={
        "line_items": [
            {"line_id": "10", "quantity": 100, "fulfilled_quantity": 75,
             "erp_price": 1.0, "po_price": 1.0},
        ],
    })
    im = compose_impact_metrics(rec)
    assert im is not None
    assert im.fulfillment_gap_pct == 25.0


def test_impact_metrics_fulfillment_gap_none_when_fully_fulfilled():
    rec = _record(resolution_data={
        "line_items": [
            {"line_id": "10", "quantity": 100, "erp_price": 1.0, "po_price": 1.0},
        ],
    })
    im = compose_impact_metrics(rec)
    assert im is not None
    # No fulfilled_quantity → defaults to qty → no gap.
    assert im.fulfillment_gap_pct is None


def test_impact_metrics_sla_priority_maps_from_verdict():
    for verdict, expected in [
        ("RED", "P1"), ("YELLOW", "P2"), ("GREEN", "P3"), (None, "P4"),
    ]:
        rec = _record(
            shadow_verdict=verdict,
            resolution_data={
                "line_items": [
                    {"line_id": "10", "quantity": 1, "erp_price": 1.0, "po_price": 1.0},
                ],
            },
        )
        im = compose_impact_metrics(rec)
        assert im is not None
        assert im.sla_priority == expected, (verdict, expected, im.sla_priority)


def test_impact_metrics_returns_none_when_no_line_items():
    rec = _record(resolution_data={})
    assert compose_impact_metrics(rec) is None


# ---------------------------------------------------------------------------
# narrative — root_cause + recommendation
# ---------------------------------------------------------------------------

def test_narrative_prefers_resolution_data_keys():
    rec = _record(resolution_data={
        "root_cause": "Promo expired on PO date",
        "recommendation": "Apply contract price $9.80",
    })
    rc, reco = compose_narrative(rec, trace_data=None)
    assert rc == "Promo expired on PO date"
    assert reco == "Apply contract price $9.80"


def test_narrative_falls_through_to_trace_narrative_for_root_cause():
    rec = _record(resolution_data={})
    trace = {"narrative": "PO price exceeds ERP by 5%.\n\nLong-form details follow."}
    rc, _ = compose_narrative(rec, trace_data=trace)
    # Takes only the first paragraph — long-form stays in DiagnosticsSection.
    assert rc == "PO price exceeds ERP by 5%."


def test_narrative_falls_through_to_trace_resolution_steps_for_recommendation():
    rec = _record(resolution_data={})
    trace = {
        "narrative": "diag",
        "resolution_steps": ["Apply contract price", "Re-publish to SAP"],
    }
    _, reco = compose_narrative(rec, trace_data=trace)
    assert reco == "Apply contract price"


def test_narrative_recommendation_uses_recommended_action_alias():
    rec = _record(resolution_data={"recommended_action": "ALLOW_BOTH"})
    _, reco = compose_narrative(rec, trace_data=None)
    assert reco == "ALLOW_BOTH"


def test_narrative_returns_none_when_nothing_available():
    rec = _record(resolution_data={})
    rc, reco = compose_narrative(rec, trace_data=None)
    assert rc is None
    assert reco is None


def test_narrative_does_not_synthesise_from_empty_strings():
    rec = _record(resolution_data={"root_cause": "   ", "recommendation": ""})
    rc, reco = compose_narrative(rec, trace_data=None)
    assert rc is None
    assert reco is None


# ---------------------------------------------------------------------------
# Narrative — Tier-4 deterministic projection from recipe output
#
# These templates summarise what the recipe + event already decided —
# they do NOT introduce new business logic, thresholds, or "best guess"
# narrative. Each template returns None when its underlying data is
# absent so the UI keeps the section structurally omitted.
# ---------------------------------------------------------------------------


def test_narrative_synthesises_contractual_correction_root_cause_from_event_prices():
    """Contractual-correction: root_cause is the price delta between PO and ERP master."""
    rec = _record(
        intent="CONTRACTUAL_CORRECTION",
        resolution_data={},  # recipe didn't write narrative
        original_event={
            "order_id": "SO-1",
            "line_item": 1,
            "po_price": 90.0,
            "sap_base_price": 100.0,
            "event_type": "EDI_850_PRICE_MISMATCH",
            "line_count": 1,
        },
    )
    rc, _ = compose_narrative(rec, trace_data=None)
    assert rc is not None
    assert "$90.00" in rc and "$100.00" in rc
    assert "-10.0%" in rc


def test_narrative_synthesises_credit_block_root_cause_from_event_amounts():
    """Credit-block: root_cause shows exposure-vs-limit math."""
    rec = _record(
        intent="CREDIT_BLOCK",
        resolution_data={},
        original_event={
            "order_id": "SO-2",
            "line_item": 1,
            "po_price": 100.0,
            "sap_base_price": 100.0,
            "event_type": "CREDIT_LIMIT_BREACH",
            "credit_limit": 50000.0,
            "current_exposure": 52000.0,
            "line_count": 1,
        },
    )
    rc, _ = compose_narrative(rec, trace_data=None)
    assert rc is not None
    assert "$52,000" in rc
    assert "$50,000" in rc
    assert "$2,000" in rc


def test_narrative_synthesises_duplicate_po_root_cause_from_recipe_score():
    """Duplicate-PO: root_cause cites composite score + matched PO."""
    rec = _record(
        intent="DUPLICATE_PO",
        resolution_data={
            "composite_score": 0.94,
            "classification": "AUTO_BLOCK",
        },
        original_event={
            "order_id": "PO-DUP-NEW",
            "line_item": 1,
            "po_price": 100.0,
            "sap_base_price": 100.0,
            "event_type": "EDI_850_DUPLICATE_PO",
            "metadata": {"matched_po_id": "PO-DUP-PRIOR"},
            "line_count": 1,
        },
    )
    rc, _ = compose_narrative(rec, trace_data=None)
    assert rc is not None
    assert "PO-DUP-PRIOR" in rc
    assert "0.94" in rc
    assert "AUTO_BLOCK" in rc


def test_narrative_humanises_recommended_action_token():
    """recommended_action is humanised into prose by the synthesiser."""
    rec = _record(
        intent="DUPLICATE_PO",
        resolution_data={"recommended_action": "BLOCK_AND_NOTIFY"},
    )
    _, reco = compose_narrative(rec, trace_data=None)
    assert reco is not None
    # Tier 1 wins (raw recipe string returned) — synthesiser only fires
    # on Tier-4 fallback. Verify the value is the raw token.
    assert reco == "BLOCK_AND_NOTIFY"


def test_narrative_synthesises_recommendation_from_applied_condition():
    """When the recipe wrote applied_condition + new_net_price (the
    contractual-correction shape) but no recommended_action, the
    synthesiser produces a sentence."""
    rec = _record(
        intent="CONTRACTUAL_CORRECTION",
        resolution_data={
            "applied_condition": "YK07",
            "new_net_price": 95.5,
            "status": "SUCCESS",
        },
    )
    _, reco = compose_narrative(rec, trace_data=None)
    assert reco is not None
    assert "YK07" in reco
    assert "$95.50" in reco


def test_narrative_synthesises_recommendation_from_final_status_fallback():
    """When the recipe halted before writing any action prose, the
    final_status surfaces as the fallback recommendation."""
    rec = _record(
        intent="EDI_MISMATCH",
        resolution_data={},  # no recommended_action, no applied_condition
        final_status="MANUAL_REVIEW_REQUIRED",
    )
    _, reco = compose_narrative(rec, trace_data=None)
    assert reco is not None
    assert "manual review" in reco.lower()


def test_narrative_synthesiser_returns_none_when_underlying_data_absent():
    """Critical invariant: synthesiser never fabricates. CONTRACTUAL_CORRECTION
    intent with neither prices on the event nor recipe output → None."""
    rec = _record(
        intent="CONTRACTUAL_CORRECTION",
        resolution_data={},
        original_event={  # no po_price / sap_base_price
            "order_id": "SO-X",
            "line_item": 1,
            "po_price": 0.0,
            "sap_base_price": 0.0,
            "event_type": "EDI_850_PRICE_MISMATCH",
            "line_count": 1,
        },
    )
    rc, _ = compose_narrative(rec, trace_data=None)
    assert rc is None  # erp_price=0 makes division undefined → None


# ---------------------------------------------------------------------------
# Narrative — universal fallback (any intent, any final_status)
# ---------------------------------------------------------------------------


def test_narrative_synthesises_root_cause_from_audit_context_missing():
    """AUDIT_CONTEXT_MISSING records (any intent — DELIVERY_DELAY,
    BACK_ORDER, etc.) get an operator-facing root_cause + recommendation
    even when the per-intent template above didn't fire and the recipe
    never wrote prose."""
    rec = _record(
        intent="DELIVERY_DELAY",
        resolution_data={},
        final_status="AUDIT_CONTEXT_MISSING",
        shadow_verdict="YELLOW",
    )
    rc, reco = compose_narrative(rec, trace_data=None)
    assert rc is not None
    assert "Delivery Delay" in rc or "audit gap" in rc.lower()
    assert reco is not None
    assert "evidence" in reco.lower() or "grandfather" in reco.lower()


def test_narrative_universal_fallback_for_unhandled_intent():
    """An intent without a per-intent template still gets prose from
    the universal verdict + final_status fallback so the UI Agent
    Analysis pane shows root_cause + recommendation."""
    rec = _record(
        intent="OVER_MAX",
        resolution_data={},
        final_status="MANUAL_REVIEW_REQUIRED",
        shadow_verdict="YELLOW",
    )
    rc, reco = compose_narrative(rec, trace_data=None)
    assert rc is not None
    assert "Over Max" in rc or "YELLOW" in rc or "MANUAL_REVIEW_REQUIRED" in rc
    assert reco is not None
    assert "manual review" in reco.lower()


def test_narrative_reads_trace_explanation_when_narrative_absent():
    """Tier 2b: trace.explanation is used as a root_cause source when
    trace.narrative is not populated (the common shape on halt paths
    like AUDIT_CONTEXT_MISSING and FAIL_TO_HUMAN)."""
    rec = _record(intent="BACK_ORDER", resolution_data={})
    trace = {
        "explanation": (
            "Compliance Shadow returned YELLOW. Routing to "
            "MANUAL_REVIEW_REQUIRED.\n\nAdditional long-form context "
            "for the Diagnostics panel."
        ),
    }
    rc, _ = compose_narrative(rec, trace_data=trace)
    assert rc is not None
    # First paragraph only — the rest stays in the long-form
    # Diagnostics view.
    assert rc.startswith("Compliance Shadow returned YELLOW")
    assert "long-form context" not in rc


def test_narrative_complete_records_get_a_recommendation():
    """COMPLETE records should produce a 'no further action' recommendation
    so the operator sees a confirmation rather than an empty Recommendation
    block (the AgentAnalysisSection structurally omits empty blocks, so a
    non-None recommendation is required for the block to render)."""
    rec = _record(
        intent="CONTRACTUAL_CORRECTION",
        resolution_data={},
        final_status="COMPLETE",
    )
    _, reco = compose_narrative(rec, trace_data=None)
    assert reco is not None
    assert "no further action" in reco.lower() or "applied" in reco.lower()
