"""Council 2026-06-07 — presentation contract composer tests.

Locks the deterministic placement projection the UI honors (asoe-ui
Guardrail #0): the `show_intent` discriminator and the audit bundle.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from contracts.models import Intent
from api.presentation_composer import (
    _NON_DISCRIMINATING_INTENTS,
    compose_presentation,
    intent_discriminates,
)


def _record(
    intent=None,
    recipe=None,
    enrichment_context=None,
    *,
    event_type=None,
    shadow_verdict=None,
    supergroup_code=None,
    trace_id=None,
):
    return SimpleNamespace(
        intent=intent,
        selected_recipe=recipe,
        enrichment_context=enrichment_context,
        event_type=event_type,
        shadow_verdict=shadow_verdict,
        supergroup_code=supergroup_code,
        trace_id=trace_id,
    )


@pytest.mark.parametrize(
    "intent,expected",
    [
        ("CREDIT_BLOCK", True),
        ("DUPLICATE_PO", True),
        ("PRICE_HOLD_RELEASE", True),
        ("EDI_MISMATCH", True),
        ("BACK_ORDER", True),
        ("OVER_MAX", True),
        # Channel/intake restatements — do NOT discriminate.
        ("MANUAL_ORDER_INTAKE", False),
        ("UNKNOWN", False),
        (None, False),
        ("", False),
    ],
)
def test_intent_discriminates(intent, expected):
    assert intent_discriminates(intent) is expected


def test_every_intent_enum_value_is_classified():
    """Each canonical Intent is either discriminating or explicitly
    non-discriminating — no value falls through ambiguously. (A new
    problem intent defaults to discriminating, which is the safe L1
    direction; this test documents the current split.)"""
    for member in Intent:
        discriminates = intent_discriminates(member.value)
        in_nondiscriminating = member.value in _NON_DISCRIMINATING_INTENTS
        # discriminating XOR listed-as-non-discriminating
        assert discriminates != in_nondiscriminating


def test_compose_presentation_problem_intent():
    c = compose_presentation(_record(intent="CREDIT_BLOCK", recipe="CreditHoldReleaseRecipe"))
    assert c.show_intent is True
    assert c.audit.recipe_name == "CreditHoldReleaseRecipe"
    assert c.audit.intent_code == "CREDIT_BLOCK"


def test_compose_presentation_channel_intent_hides_intent_but_keeps_audit():
    c = compose_presentation(
        _record(intent="MANUAL_ORDER_INTAKE", recipe="ManualOrderIntakeRecipe")
    )
    # Q3: the channel-intent is NOT shown in L1 ...
    assert c.show_intent is False
    # ... but the raw enum + recipe are never lost — they're in the audit
    # bundle (Guardrail #7: available, not removed).
    assert c.audit.intent_code == "MANUAL_ORDER_INTAKE"
    assert c.audit.recipe_name == "ManualOrderIntakeRecipe"


def test_compose_presentation_missing_fields():
    c = compose_presentation(_record())
    assert c.show_intent is False
    assert c.audit.recipe_name is None
    assert c.audit.intent_code is None
    assert c.situation_headline is None


def test_situation_headline_reuses_governed_one_liner():
    """The Situation headline is the SAME governed per-intent one-liner
    the queue rows use — not a separately-authored string (single source
    of truth, no drift)."""
    rec = _record(
        intent="MANUAL_ORDER_INTAKE",
        recipe="ManualOrderIntakeRecipe",
        enrichment_context={
            "email_source": {
                "subject": "PO 7781 — 200 cases",
                "classification": "NEW_ORDER",
            }
        },
    )
    c = compose_presentation(rec)
    assert c.situation_headline == "New Order: PO 7781 — 200 cases"


def test_compose_presentation_emits_section_tiers():
    # The composer projects the full placement authority so the UI can
    # route any rendered section deterministically.
    c = compose_presentation(_record(intent="CREDIT_BLOCK"))
    assert c.section_tiers["price_analysis"] == "evidence"
    assert c.section_tiers["edi_850_audit"] == "audit"
    assert c.section_tiers["knowledge_graph"] == "audit"


def test_situation_headline_none_when_template_sparse():
    # MANUAL_ORDER_INTAKE with no email-source enrichment → no honest
    # headline → None (structurally omitted on the UI, never fabricated).
    c = compose_presentation(_record(intent="MANUAL_ORDER_INTAKE"))
    assert c.situation_headline is None


# ── Provenance card (council 2026-06-10) ──────────────────────────────
# The Diagnostics & Audit "Provenance" bundle is a pure projection of
# already-decided record fields. These lock that projection: present →
# emitted; absent → None (UI omits the row, never fabricates).


def test_provenance_projects_record_fields():
    c = compose_presentation(
        _record(
            intent="DUPLICATE_PO",
            recipe="DuplicatePORecipe",
            event_type="EDI_850_PRICE_MISMATCH",
            shadow_verdict="YELLOW",
            supergroup_code="cpg.beverage.dup-po",
            trace_id="8c9f-f828-41d0-9a2b",
        )
    )
    a = c.audit
    assert a.recipe_name == "DuplicatePORecipe"
    assert a.intent_code == "DUPLICATE_PO"
    assert a.event_type == "EDI_850_PRICE_MISMATCH"
    assert a.shadow_verdict == "YELLOW"
    assert a.supergroup_code == "cpg.beverage.dup-po"
    assert a.correlation_id == "8c9f-f828-41d0-9a2b"


def test_provenance_taxonomy_version_emitted_only_with_classification():
    from contracts._generated.taxonomy_constants import TAXONOMY_VERSION

    # Classified → taxonomy version snapshot travels with the supergroup.
    classified = compose_presentation(
        _record(intent="CREDIT_BLOCK", supergroup_code="cpg.credit.block")
    )
    assert classified.audit.taxonomy_version == TAXONOMY_VERSION

    # Unclassified → no supergroup → version is None (UI omits the row;
    # a bare version with no classification would be misleading provenance).
    unclassified = compose_presentation(_record(intent="CREDIT_BLOCK"))
    assert unclassified.audit.supergroup_code is None
    assert unclassified.audit.taxonomy_version is None


def test_provenance_all_none_when_record_bare():
    # A record with no provenance fields set yields an all-None bundle —
    # every row structurally omitted on the UI, never a fabricated value.
    a = compose_presentation(_record(intent="CREDIT_BLOCK")).audit
    assert a.event_type is None
    assert a.shadow_verdict is None
    assert a.supergroup_code is None
    assert a.taxonomy_version is None
    assert a.correlation_id is None


# ── Situation sub-line (audit finding #2, option C — sign-off 2026-06-10) ──
# The sub-line's membership is backend-owned: SLA (from the parent case)
# + lifecycle state only. The $ figure deliberately does NOT ride here —
# it keeps its single home in impact_metrics / ImpactBar.


def test_situation_context_projects_case_sla_and_record_state():
    rec = _record(intent="CREDIT_BLOCK")
    rec.lifecycle_state = "PENDING_REVIEW"
    case = SimpleNamespace(sla_due_at="2026-06-10T18:30:00+00:00")
    sc = compose_presentation(rec, case).situation_context
    assert sc.sla_due_at == "2026-06-10T18:30:00+00:00"
    assert sc.lifecycle_state == "PENDING_REVIEW"


def test_situation_context_sla_none_without_parent_case():
    # Tier-1 stateless records (no parent OrderCase) → no SLA segment;
    # lifecycle state still rides (it lives on the record itself).
    rec = _record(intent="CREDIT_BLOCK")
    rec.lifecycle_state = "RESOLVED"
    sc = compose_presentation(rec, None).situation_context
    assert sc.sla_due_at is None
    assert sc.lifecycle_state == "RESOLVED"


def test_situation_context_emits_timestamp_not_rendered_label():
    # The contract carries the ISO timestamp, never a pre-rendered
    # relative label ("due in 3h 12m") that would go stale — the UI's
    # SLA ticker owns live formatting.
    case = SimpleNamespace(sla_due_at="2026-06-11T09:00:00+00:00")
    sc = compose_presentation(_record(intent="DUPLICATE_PO"), case).situation_context
    assert sc.sla_due_at == "2026-06-11T09:00:00+00:00"


def test_situation_context_all_none_when_bare():
    # Bare record, no case → both facts None → the UI structurally
    # omits the whole sub-line (no placeholder, no fabricated text).
    sc = compose_presentation(_record()).situation_context
    assert sc.sla_due_at is None
    assert sc.lifecycle_state is None
