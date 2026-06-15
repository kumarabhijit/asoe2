"""ADR-041 P3e Phase 0b T2c — per-intent verdict-color override gates.

The raw severity-wins rollup in `api/case_summary.py::
_verdict_color_rollup` returns R > A > G based on child shadow
verdicts alone. The 2026-05-28 Recipe SME panel §5 surfaced
intent-specific gates that override the raw color in two
directions:

  * **Never RED at intake** — for intents where the recipe
    over-flags during initial classification (the operator gets
    a RED row that almost always resolves green). Ceiling at
    AMBER until a stronger signal (SLA breach, severity > N)
    fires.

  * **Never GREEN** — for intents where SOX / four-eyes /
    high-value gates demand a human eye regardless of agent
    confidence. Floor at AMBER.

Gates are deterministic, pure projections over the primary
ChildCase + the parent OrderCase. No I/O, no recipe execution.

The 8 rules below are verbatim from the Recipe SME panel
response; each carries the panel-cited justification in the
docstring. New gates land here when the panel surfaces them.

Recipe SME §5 rules:

NEVER RED (ceiling at AMBER):
  1. DELIVERY_DELAY days_late > 0 but no SLA breach — routine
     reschedule; RED is theatre.
  2. PALLET BROKEN_LAYER / PARTIAL_PALLET — operationally
     trivial.
  3. EDI_MISMATCH sub_type ∈ {DATE_FORMAT, UOM_NORMALISATION} —
     classifier-time corrections.

NEVER GREEN (floor at AMBER):
  4. PRICE_HOLD AUTO_RELEASE near tolerance — SOX-touchy
     near-ceiling release.
  5. DUPLICATE_PO when duplicate_order.total_value > $10k —
     credit-block-adjacent, large auto-cancel needs sign-off.
  6. Tier-1 customer (any intent) — VIPs don't get silent
     auto-resolves.
  7. EMAIL_COMPLAINT / CHANGE_ANALYSIS with
     decision.requires_cosign=true — recipe already says cosign.
"""

from __future__ import annotations

from typing import Literal, Optional


VerdictColor = Literal["R", "A", "G"]


# Customer tiers that NEVER get silent GREEN auto-resolves (Recipe
# SME §5 rule 6). VIP / strategic-account customers — operator
# attention is the policy.
NEVER_GREEN_CUSTOMER_TIERS: frozenset[int] = frozenset({1})


# PRICE_HOLD AUTO_RELEASE-near-tolerance threshold. The recipe sets
# release at the contract tolerance ceiling; this gate fires when
# the variance is within 20% of that ceiling (i.e., >= 80% of
# tolerance), which Recipe SME §5 cited as the auditor-flag band.
PRICE_HOLD_NEAR_CEILING_FRACTION: float = 0.8


# DUPLICATE_PO floor amount — duplicates above this value never
# ship GREEN (auto-cancel of a 5-figure SO needs human sign-off).
DUPLICATE_PO_GREEN_FLOOR_USD: float = 10_000.0


def apply_verdict_color_gates(
    raw_color: Optional[VerdictColor],
    *,
    intent: Optional[str],
    primary_record,
    case,
) -> Optional[VerdictColor]:
    """Apply the panel-ratified per-intent verdict-color overrides.

    Pure projection. Takes the raw severity-wins rollup and the
    primary ChildCase + OrderCase; returns the gated color. None
    in → None out (the case has no children yet, so neither raw
    nor gated color is meaningful).
    """
    if raw_color is None:
        return None
    if intent is None or primary_record is None:
        return raw_color

    # NEVER GREEN gates apply first — they're SOX-relevant
    # invariants that override even YELLOW (lift to AMBER).
    if raw_color == "G" and _should_floor_to_amber(intent, primary_record, case):
        return "A"

    # NEVER RED gates ceiling at AMBER — they apply only when
    # the raw color is R, since the gate is "don't escalate
    # below-threshold cases."
    if raw_color == "R" and _should_ceiling_at_amber(intent, primary_record):
        return "A"

    return raw_color


# ---------------------------------------------------------------------------
# NEVER GREEN — floor-to-amber
# ---------------------------------------------------------------------------


def _should_floor_to_amber(intent: str, record, case) -> bool:
    """Rule 4: PRICE_HOLD_RELEASE AUTO_RELEASE near tolerance."""
    # Keyed by the runtime Intent enum value ("PRICE_HOLD_RELEASE"), not the
    # SME panel's "PRICE_HOLD" label — the prior string never matched a real
    # record, so this SOX floor-to-AMBER gate had silently stopped firing.
    if intent == "PRICE_HOLD_RELEASE" and _price_hold_near_ceiling(record):
        return True

    """Rule 5: DUPLICATE_PO with duplicate_order.total_value > $10k."""
    if intent == "DUPLICATE_PO" and _duplicate_po_above_floor(record):
        return True

    """Rule 6: tier-1 customers — applies across ALL intents."""
    if _is_high_tier_customer(case):
        return True

    """Rule 7: EMAIL_COMPLAINT / CHANGE_ANALYSIS with requires_cosign=true."""
    if intent in ("EMAIL_COMPLAINT", "CHANGE_ANALYSIS") and _requires_cosign(record):
        return True

    return False


def _price_hold_near_ceiling(record) -> bool:
    """PRICE_HOLD recipe writes the auto-release decision into
    `resolution_data["price_hold_analysis"]`. We flag when:
      * action == AUTO_RELEASE
      * variance_pct is within PRICE_HOLD_NEAR_CEILING_FRACTION
        of tolerance_pct
    Returns False on any missing field — defensive when the
    recipe surface hasn't been wired."""
    rd = getattr(record, "resolution_data", None) or {}
    hold = rd.get("price_hold_analysis") if isinstance(rd, dict) else None
    if not isinstance(hold, dict):
        return False
    if hold.get("action") != "AUTO_RELEASE":
        return False
    try:
        variance_pct = float(hold.get("variance_pct", 0))
        tolerance_pct = float(hold.get("tolerance_pct", 0))
    except (TypeError, ValueError):
        return False
    if tolerance_pct <= 0:
        return False
    return variance_pct >= tolerance_pct * PRICE_HOLD_NEAR_CEILING_FRACTION


def _duplicate_po_above_floor(record) -> bool:
    """DuplicatePORecipe writes the matched-PO snapshot into
    `resolution_data["duplicate_detection"].duplicate_order`. We
    flag when total_value > DUPLICATE_PO_GREEN_FLOOR_USD."""
    rd = getattr(record, "resolution_data", None) or {}
    detection = rd.get("duplicate_detection") if isinstance(rd, dict) else None
    if not isinstance(detection, dict):
        return False
    dup = detection.get("duplicate_order")
    if not isinstance(dup, dict):
        return False
    try:
        total = float(dup.get("total_value", 0))
    except (TypeError, ValueError):
        return False
    return total > DUPLICATE_PO_GREEN_FLOOR_USD


def _is_high_tier_customer(case) -> bool:
    """OrderCase.tier is the materialisation-policy tier (ADR-038
    §7.1). Tier 1 is the VIP / strategic-account band per Recipe
    SME §5 rule 6 — silent GREEN auto-resolves are blocked for
    these customers regardless of intent."""
    tier = getattr(case, "tier", None)
    if tier is None:
        return False
    try:
        return int(tier) in NEVER_GREEN_CUSTOMER_TIERS
    except (TypeError, ValueError):
        return False


def _requires_cosign(record) -> bool:
    """CHANGE_ANALYSIS writes `enrichment_context["change_analysis"]
    .decision.requires_cosign`. EMAIL_COMPLAINT routes through
    ChangeAnalysisRecipe for the order-change shape, so the same
    field shows up under the same key. Returns True only on
    explicit True; missing field is treated as not-required."""
    enrichment = getattr(record, "enrichment_context", None) or {}
    if not isinstance(enrichment, dict):
        return False
    change = enrichment.get("change_analysis")
    if not isinstance(change, dict):
        return False
    decision = change.get("decision")
    if not isinstance(decision, dict):
        return False
    return decision.get("requires_cosign") is True


# ---------------------------------------------------------------------------
# NEVER RED — ceiling-at-amber
# ---------------------------------------------------------------------------


# Recipe SME §5 rule 3 — EDI_MISMATCH sub-types that should never
# ship RED at intake. Classifier-time corrections; routine ops.
NEVER_RED_EDI_SUBTYPES: frozenset[str] = frozenset({
    "DATE_FORMAT", "UOM_NORMALISATION",
})

# Recipe SME §5 rule 2 — PALLET classifications that should never
# ship RED. Operationally trivial; YELLOW max.
NEVER_RED_PALLET_CLASSIFICATIONS: frozenset[str] = frozenset({
    "BROKEN_LAYER", "PARTIAL_PALLET",
})


def _should_ceiling_at_amber(intent: str, record) -> bool:
    """Rule 1: DELIVERY_DELAY days_late > 0 but no SLA breach."""
    if intent == "DELIVERY_DELAY" and _delivery_delay_within_sla(record):
        return True

    """Rule 2: PALLET_CONFIG BROKEN_LAYER / PARTIAL_PALLET classifications."""
    # Runtime Intent enum value ("PALLET_CONFIG"), not the SME "PALLET" label
    # — the prior string never matched, so this ceiling-at-AMBER gate had
    # silently stopped firing for real pallet records.
    if intent == "PALLET_CONFIG" and _pallet_routine_class(record):
        return True

    """Rule 3: EDI_MISMATCH DATE_FORMAT / UOM_NORMALISATION sub-types."""
    if intent == "EDI_MISMATCH" and _edi_routine_subtype(record):
        return True

    return False


def _delivery_delay_within_sla(record) -> bool:
    """Days_late > 0 without an SLA-breach flag means routine
    reschedule (Recipe SME §5 rule 1). The `sla_breach` flag is
    set by adapt_delivery_delay's SLA-contract gateway when
    available; absence means "no breach known"."""
    rd = getattr(record, "resolution_data", None) or {}
    delay = rd.get("delivery_delay_analysis") if isinstance(rd, dict) else None
    if not isinstance(delay, dict):
        # Fallback: read from resolution_data top-level (the
        # adapter's synthesise path writes there).
        delay = rd if isinstance(rd, dict) else {}
    try:
        days_late = int(delay.get("days_late", 0))
    except (TypeError, ValueError):
        return False
    if days_late <= 0:
        return False
    sla_breach = delay.get("sla_breach")
    # If the gate hasn't fired (SLA contract not wired), treat
    # as no-breach (the gate's job is to demote RED until breach
    # is positively confirmed).
    return sla_breach is not True


def _pallet_routine_class(record) -> bool:
    rd = getattr(record, "resolution_data", None) or {}
    pallet = rd.get("pallet_analysis") if isinstance(rd, dict) else None
    if not isinstance(pallet, dict):
        return False
    classification = pallet.get("classification")
    return classification in NEVER_RED_PALLET_CLASSIFICATIONS


def _edi_routine_subtype(record) -> bool:
    rd = getattr(record, "resolution_data", None) or {}
    edi = rd.get("edi_mismatch_analysis") if isinstance(rd, dict) else None
    if not isinstance(edi, dict):
        return False
    sub_type = edi.get("sub_type")
    return sub_type in NEVER_RED_EDI_SUBTYPES
