"""Presentation contract composer (council 2026-06-07).

Computes the deterministic `PresentationContract` the UI projects onto
the exception detail surface. Placement of contested elements is a
backend decision, not per-session UI taste (asoe-ui Guardrail #0): the
UI honors what this composer emits and never re-derives it.

Pure functions — no I/O. The single source of truth for the
`show_intent` discriminator and the audit bundle.
"""

from __future__ import annotations

from typing import Any

from api.schemas import PresentationAudit, PresentationContract


# Intents that merely restate the arrival / intake channel rather than
# name a decision-discriminating problem. The operator's own question
# (council 2026-06-07): "every customer-inbox order is classified
# MANUAL_ORDER_INTAKE — what does showing that add?" Nothing. These stay
# out of Layer 1; the raw enum is always available in the audit bundle.
# UNKNOWN is non-discriminating by definition (it names nothing).
#
# Everything else in `contracts.models.Intent` (CREDIT_BLOCK,
# DUPLICATE_PO, PRICE_HOLD_RELEASE, EDI_MISMATCH, BACK_ORDER, OVER_MAX,
# MIN_ORDER_QTY, PALLET_CONFIG, DELIVERY_DELAY, MASS_PRICING_ERROR,
# CONTRACTUAL_CORRECTION) names a problem and DOES discriminate.
_NON_DISCRIMINATING_INTENTS: frozenset[str] = frozenset(
    {"MANUAL_ORDER_INTAKE", "UNKNOWN"}
)


def intent_discriminates(intent: Any) -> bool:
    """True when the classified intent names a problem the operator acts
    on (so it earns a place in Layer 1), False when it merely restates
    the arrival channel or is absent/unknown."""
    if not intent:
        return False
    return str(intent) not in _NON_DISCRIMINATING_INTENTS


def compose_presentation(record: Any) -> PresentationContract:
    """Project a record into its presentation contract.

    Pure projection over already-decided record fields — no recipe
    execution, no shadow re-evaluation (Verdict 2026-04-22: the
    composer assembles, recipes do not).
    """
    intent = getattr(record, "intent", None)
    # Reuse the governed per-intent one-liner — the SAME plain-language
    # the /cases queue rows show — so the Situation headline never
    # drifts from queue vocabulary. Lazy import mirrors compose_case_summary
    # (avoids a module-load cycle). `case` is unused by every template's
    # field projection, so None is safe.
    from api.case_summary_templates import render_template

    headline = render_template(record, None).one_liner

    from api.presentation_tiers import section_tiers

    # Taxonomy classification node assigned to this record. The deployed
    # taxonomy version is only meaningful provenance alongside an actual
    # classification, so it is emitted only when supergroup is present
    # (else None → UI omits the row, never a fabricated version).
    supergroup = getattr(record, "supergroup_code", None)
    from contracts._generated.taxonomy_constants import TAXONOMY_VERSION

    return PresentationContract(
        situation_headline=headline,
        show_intent=intent_discriminates(intent),
        section_tiers=section_tiers(),
        audit=PresentationAudit(
            recipe_name=getattr(record, "selected_recipe", None),
            intent_code=intent,
            # Provenance card (council 2026-06-10) — pure projections of
            # already-decided record fields; None when absent.
            event_type=getattr(record, "event_type", None),
            shadow_verdict=getattr(record, "shadow_verdict", None),
            supergroup_code=supergroup,
            taxonomy_version=TAXONOMY_VERSION if supergroup else None,
            correlation_id=getattr(record, "trace_id", None),
        ),
    )
