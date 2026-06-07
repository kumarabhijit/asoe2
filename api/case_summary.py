"""ADR-041 P3e §3.1 — CaseSummary projection.

This module computes the read-projection fields the `/cases` queue
row consumes on the asoe-ui side (the seven new fields on the
widened `CaseListItem`). Pure projection — no recipe execution, no
shadow re-evaluation. Each invocation reads an `OrderCase` plus its
attached `ChildCase` records and returns a dataclass-like result
the route layer serialises into the wire response.

Per ADR-041 P3e §3.3 and the 2026-05-28 Recipe SME panel response,
two known recipe gaps ship as `null` until the corresponding recipe
output extension lands (tracked under
`compliance/audit_bearing_registry.yaml::grandfather_clauses`):

  * PRICE_HOLD — `PriceHoldAnalysisData` carries no `sku` / `qty`;
    we cannot honestly compute `top_line_sku_*` or `dollar_impact`.
  * EMAIL_COMPLAINT (pure intake, no `change_analysis` block) — no
    quantity fields in `EmailOrderEntryAnalysisData`; we cannot
    compute `problem_one_liner`.

For both, the projection returns `null` rather than synthesising —
Guardrail #6 (no partial-truth states) and the Verdict 2026-04-22
"build_analysis is the sole composer" rule apply.

The first version of this module ships ONLY the always-available
fields:

  * `audit_verdict_color`     ← rollup of child shadow verdicts
                                (R if any RED, A if any YELLOW,
                                G otherwise)
  * `intent`                  ← the primary child's intent
                                (highest financial_impact_usd, or
                                first child when no impact is set)
  * `customer_name`           ← OrderCase.customer_id (display
                                fallback until a customer-service
                                lookup lands; cosmetic field, so
                                EvidenceBlock optional-bypass on
                                the UI side handles a future swap)
  * `dollar_impact`           ← primary child's
                                `resolution_data["financial_impact_usd"]`
                                converted to cents; currency
                                defaults to `"USD"` for the v1
                                projection (multi-currency lands
                                with the explicit-currency field
                                addition to resolution_data)

The per-intent `problem_one_liner` and `top_line_sku_*` are
deferred to the Recipe team's per-intent template implementation;
this module returns `None` for those and explicitly does NOT
synthesise from event metadata or recipe scratch fields.

Recipe SME's per-intent verdict-color gates (never-RED, never-GREEN
rules) are NOT applied here in v1; this module returns the raw
rollup color. The gate rules live in the recipe layer and will be
applied at `build_case_summary` graph-node time once the gates ship
as a registry table. Tracked in
`docs/specs/case-summary-projection.md` T2 §"Audit verdict color
gates".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, Optional


VerdictColor = Literal["R", "A", "G"]

# ---------------------------------------------------------------------------
# Attention disposition (council 2026-06-07, Reis ruling)
# ---------------------------------------------------------------------------
# "Does this case need a human?" is a backend-owned disposition, NOT a
# UI inference. The /cases queue groups by `attention_state` so it can
# separate work awaiting human judgement from auto-resolved / in-flight
# work; the UI must NOT switch on `status` to derive this (asoe-ui
# Guardrail #1, and the detection/resolution boundary stays backend-
# owned).
#
#   * NEEDS_HUMAN — the operator must act or be aware: an awaiting-human
#     review, a FAILED pipeline (FAIL_TO_HUMAN), or a compliance BLOCK.
#     A control tower foregrounds compliance blocks — hiding a RED-
#     blocked financial action would defeat its purpose (Majors).
#   * IN_FLIGHT   — agents/systems/external parties are working; no
#     operator action is pending right now.
#   * DONE        — resolved; informational only.
#
# This mapping IS the policy. Changing the disposition of a lifecycle
# state is a one-line edit here, open to compliance refinement; it is
# deliberately the only place the mapping lives.
AttentionState = Literal["NEEDS_HUMAN", "IN_FLIGHT", "DONE"]

_ATTENTION_BY_STATUS: dict[str, AttentionState] = {
    "OPEN_AWAITING_HUMAN": "NEEDS_HUMAN",
    "FAILED": "NEEDS_HUMAN",
    "BLOCKED": "NEEDS_HUMAN",
    "OPEN_AGENT_PROCESSING": "IN_FLIGHT",
    "OPEN_AWAITING_BUYER": "IN_FLIGHT",
    "OPEN_AWAITING_ERP": "IN_FLIGHT",
    "RESOLVED": "DONE",
}


def _attention_of(case) -> AttentionState:
    """Deterministic case lifecycle → attention disposition.

    Unknown / unmapped states default to NEEDS_HUMAN: an unclassifiable
    case is exactly what a human should look at, never something the
    queue silently buries. Pure function — no I/O.
    """
    return _ATTENTION_BY_STATUS.get(getattr(case, "status", None), "NEEDS_HUMAN")


@dataclass(frozen=True)
class DollarImpact:
    """Wire shape — mirrors
    `asoe-ui/src/lib/api.ts::CaseSummaryDollarImpact`.

    `amount_cents` is the integer cent value (avoids float drift
    across the JSON boundary); `currency` is ISO 4217. Both fields
    are required — partial-truth amounts without currency are a
    Compliance Guardrail #6 violation.
    """

    amount_cents: int
    currency: str

    def to_dict(self) -> dict:
        return {"amount_cents": self.amount_cents, "currency": self.currency}


@dataclass(frozen=True)
class CaseSummary:
    """The seven CaseSummary projection fields. All nullable —
    callers (the route serialiser) project these onto the
    `CaseListItem` wire shape; `null` becomes Structurally Omitted
    on the UI's `<EvidenceBlock>`.
    """

    customer_name: Optional[str]
    top_line_sku_code: Optional[str]
    top_line_sku_title: Optional[str]
    problem_one_liner: Optional[str]
    intent: Optional[str]
    dollar_impact: Optional[DollarImpact]
    audit_verdict_color: Optional[VerdictColor]
    # Council 2026-06-07 — backend-owned needs-human vs already-done
    # disposition the /cases queue groups by. Always present (never
    # null): every case has a lifecycle state, so it always has a
    # disposition.
    attention_state: AttentionState

    def to_dict(self) -> dict:
        """Serialise to the wire shape consumed by `CaseListItem`."""
        return {
            "customer_name": self.customer_name,
            "top_line_sku_code": self.top_line_sku_code,
            "top_line_sku_title": self.top_line_sku_title,
            "problem_one_liner": self.problem_one_liner,
            "intent": self.intent,
            "dollar_impact": (
                self.dollar_impact.to_dict() if self.dollar_impact else None
            ),
            "audit_verdict_color": self.audit_verdict_color,
            "attention_state": self.attention_state,
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_case_summary(case, records: Iterable) -> CaseSummary:
    """Project an `OrderCase` + its attached `ChildCase` records
    into a `CaseSummary`.

    Pure function — no side effects, no I/O. Safe to call from the
    route serialiser hot path. Empty `records` is the just-opened
    case state; the projection returns mostly-null with
    `customer_name` populated from the OrderCase.
    """
    records = list(records)
    primary = _pick_primary_record(records)

    # ADR-041 P3e Phase 0b T2b — per-intent template registry
    # dispatch. Templates are pure projections over
    # `record.resolution_data` + `record.enrichment_context`
    # (Guardrail #6 — no event-metadata blending). When the intent
    # has no template OR the recipe output is too sparse, all
    # three fields stay None — the wire shape ships nulls and the
    # UI's EvidenceBlock collapses the cells.
    from api.case_summary_templates import render_template
    from api.case_summary_verdict_gates import apply_verdict_color_gates

    rendered = render_template(primary, case)
    intent = _intent_of(primary)
    raw_color = _verdict_color_rollup(records)

    # ADR-041 P3e Phase 0b T2c — per-intent verdict color
    # overrides. The raw severity-wins rollup is ceiling-ed at
    # AMBER for over-flagged intents and floored at AMBER for
    # SOX-touchy ones (Recipe SME §5).
    gated_color = apply_verdict_color_gates(
        raw_color,
        intent=intent,
        primary_record=primary,
        case=case,
    )

    return CaseSummary(
        customer_name=_customer_name(case),
        top_line_sku_code=rendered.sku_code,
        top_line_sku_title=rendered.sku_title,
        problem_one_liner=rendered.one_liner,
        intent=intent,
        dollar_impact=_dollar_impact_of(primary),
        audit_verdict_color=gated_color,
        attention_state=_attention_of(case),
    )


# ---------------------------------------------------------------------------
# Field projections
# ---------------------------------------------------------------------------


def _customer_name(case) -> Optional[str]:
    """Display name for the customer. Cosmetic field — the UI's
    EvidenceBlock tier allows the optional fall-through (Guardrail
    #6 cosmetic-bypass), so returning the customer_id when no
    richer name is available is acceptable. Falls back to None when
    customer_id itself is unset (anonymous-tenant case).
    """
    return getattr(case, "customer_id", None)


def _intent_of(record) -> Optional[str]:
    """The intent that drives the case's classification. Returns the
    primary child's intent; None when there are no children yet
    (just-opened case) or the child has no intent classified.
    """
    if record is None:
        return None
    return getattr(record, "intent", None)


def _dollar_impact_of(record) -> Optional[DollarImpact]:
    """Extract the dollar impact from the primary child's
    resolution_data. Returns None when:

      * the field is absent (intent gap — e.g., EDI_MISMATCH,
        PALLET, EMAIL_COMPLAINT-intake; see Recipe SME table for
        the explicit hide-the-column list);
      * the value is None / non-numeric (partial-truth guard);
      * the value is zero (zero-impact rows are not actionable in
        the dollar-sort triage frame; collapse the cell);
      * the currency code is malformed (must be ISO 4217 3-letter
        uppercase).

    ADR-041 P3e Phase 0b T2d — multi-currency carrier. Two source
    shapes accepted:

      1. **Preferred** — `resolution_data["financial_impact"]`
         (amount) + `resolution_data["currency"]` (ISO 4217).
         Symmetric to the CaseSummaryDollarImpact wire shape; the
         recipe writes both fields together.
      2. **Legacy** — `resolution_data["financial_impact_usd"]`
         (amount, USD assumed). Pre-T2d field; defaults the
         currency to "USD". Backward-compat for the
         pre-multi-currency recipe surface.

    When BOTH shapes are present, the preferred one wins — recipes
    migrating one at a time can land the explicit-currency field
    without removing the legacy alias.
    """
    if record is None:
        return None
    rd = getattr(record, "resolution_data", None)
    if not isinstance(rd, dict):
        return None

    # Preferred path — explicit currency carrier.
    raw_amount = rd.get("financial_impact")
    currency = rd.get("currency")
    if raw_amount is None:
        # Legacy fallback — _usd suffix implies USD.
        raw_amount = rd.get("financial_impact_usd")
        currency = currency or "USD"

    if raw_amount is None:
        return None
    try:
        amount = float(raw_amount)
    except (TypeError, ValueError):
        return None
    if amount == 0.0:
        return None

    # Currency validation — ISO 4217 is 3 uppercase letters.
    # Reject non-string / non-3-letter / mixed-case to keep the
    # wire boundary clean (Compliance Guardrail #6 — bare amount
    # without a clear currency code is a partial-truth state).
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
        return None
    currency = currency.upper()

    # Convert to cents. round() to nearest — float-truncation
    # silently underpays $0.005 amounts.
    cents = int(round(amount * 100))
    return DollarImpact(amount_cents=cents, currency=currency)


def _verdict_color_rollup(records: list) -> Optional[VerdictColor]:
    """Roll the child shadow verdicts up to a single case-level
    color. Severity wins: RED > YELLOW > GREEN. Returns None when
    there are no records OR no record has a verdict yet (case is
    still classifying).

    Recipe SME's per-intent never-RED / never-GREEN gates are NOT
    applied here in v1 — see the module docstring rationale. The
    raw rollup is the input that gate function will eventually
    consume.
    """
    if not records:
        return None
    has_red = False
    has_yellow = False
    has_green = False
    for r in records:
        v = getattr(r, "shadow_verdict", None)
        if v == "RED":
            has_red = True
        elif v == "YELLOW":
            has_yellow = True
        elif v == "GREEN":
            has_green = True
    if has_red:
        return "R"
    if has_yellow:
        return "A"
    if has_green:
        return "G"
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pick_primary_record(records: list):
    """Pick the child record that represents the case for the
    summary fields. Rule: highest `financial_impact_usd`; falls
    back to the first record (insertion order) when nothing has a
    measured impact.

    Recipe SME §4 — the "top line by dollar" rule applies; we
    surface the line driving the triage decision.
    """
    if not records:
        return None
    with_impact = []
    for r in records:
        rd = getattr(r, "resolution_data", None) or {}
        raw = rd.get("financial_impact_usd")
        try:
            amount = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            amount = None
        with_impact.append((amount, r))
    # Sort: highest impact first; None last.
    with_impact.sort(
        key=lambda pair: (pair[0] is None, -(pair[0] or 0.0)),
    )
    return with_impact[0][1]
