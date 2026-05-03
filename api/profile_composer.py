"""Profile composer — entity profile, impact metrics, narrative.

Single source of truth for the four order-level enrichment fields on
`AnalysisResponse` that are NOT per-recipe `*AnalysisData` projections:

  * `entity_profile`   — master-data context for the customer entity,
    sourced from the `Account` seed table.
  * `impact_metrics`   — quantitative blast radius (revenue at risk,
    deltas, fulfilment gap, SLA priority), computed deterministically
    from line-item totals and record metadata.
  * `root_cause`       — order-level prose root cause, sourced from
    trace `narrative` / shadow explanation. Distinct from the
    per-line-item categorical `root_cause` on `LineItem`.
  * `recommendation`   — one-line agent recommendation, sourced from
    `record.resolution_data.recommended_action` or — when absent —
    distilled from the recipe's `resolution_data.summary`.

These projections are pure, deterministic functions of the record. The
composer never invents narrative text or fabricates metrics; when the
backing data isn't there, the corresponding field returns ``None`` so
the UI structurally omits the surface (Verdict 2026-04-22 / CLAUDE.md
Guardrail #6 — no partial-truth fallbacks).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from api.schemas import EntityProfile, ImpactMetrics
from api.store import ExceptionRecord
from api.users import get_account


# ---------------------------------------------------------------------------
# Entity profile
# ---------------------------------------------------------------------------

def compose_entity_profile(record: ExceptionRecord) -> Optional[EntityProfile]:
    """Build an EntityProfile for the exception's customer.

    Uses the seed `Account` table as the master-data source. Returns
    ``None`` when neither `account_id` nor `account_name` is set on
    the record — the UI then omits the Customer column entirely
    rather than rendering an empty pane.

    `customer_tier`, `vip_status`, and `credit_standing` are
    grandfathered fields (registry-tracked) until the corresponding
    customer-master gateway lands. Today we project `tier` from the
    Account row when available; the other two stay ``None``.
    """
    acct = None
    if record.account_id:
        acct = get_account(record.account_id)
    if acct is None:
        # No account linkage → no profile. The UI's `ContextStrip`
        # treats both fields absent as "structurally omit the pane,"
        # which is the right behaviour for an exception that's not
        # tied to a known customer (rare but legal).
        if not record.account_name:
            return None
        # Synthesise a minimal profile from the denormalised
        # account_name carried on the record. bp_number is required
        # by the contract, so we use a sentinel that the UI displays
        # as-is — not a fabricated SAP ID.
        return EntityProfile(
            customer_name=record.account_name,
            bp_number="UNKNOWN",
            customer_tier=None,
            vip_status=None,
            credit_standing=None,
            location=None,
            region=None,
        )
    return EntityProfile(
        customer_name=acct.name,
        bp_number=acct.bp_number,
        customer_tier=acct.tier,
        vip_status=None,           # grandfathered — no producer
        credit_standing=None,      # grandfathered — no producer
        location=None,             # grandfathered — no producer
        region=acct.region,
    )


# ---------------------------------------------------------------------------
# Impact metrics
# ---------------------------------------------------------------------------

def compose_impact_metrics(record: ExceptionRecord) -> Optional[ImpactMetrics]:
    """Compute deterministic blast-radius metrics from line items.

    Returns ``None`` when the record carries no line-item data —
    nothing to compute, and a zero-filled struct would imply
    "verified zero impact" which is partial-truth. The UI omits
    the impact column when this returns None.
    """
    raw_items: List[Dict[str, Any]] = (
        record.resolution_data.get("line_items", []) or []
    )
    if not raw_items:
        return None

    affected_lines = len(raw_items)
    total_erp = 0.0
    total_po = 0.0
    fulfilled_qty = 0.0
    requested_qty = 0.0
    for li in raw_items:
        qty = float(li.get("quantity", 0) or 0)
        erp = float(li.get("erp_price", 0.0) or 0.0)
        po = float(li.get("po_price", 0.0) or 0.0)
        total_erp += erp * qty
        total_po += po * qty
        # Optional fulfilment fields — recipes that don't produce
        # them simply leave these at zero, which we surface as a
        # missing fulfilment_gap_pct (None) rather than a 0.0.
        requested_qty += qty
        fulfilled_qty += float(li.get("fulfilled_quantity", qty) or qty)

    delta = total_po - total_erp
    delta_pct = (delta / total_erp * 100.0) if total_erp else 0.0
    revenue_at_risk = abs(delta) if delta else total_po

    fulfillment_gap_pct: Optional[float] = None
    if requested_qty > 0 and fulfilled_qty < requested_qty:
        fulfillment_gap_pct = round(
            (requested_qty - fulfilled_qty) / requested_qty * 100.0, 2
        )

    sla_priority = _sla_priority_for(record)

    return ImpactMetrics(
        revenue_at_risk=round(revenue_at_risk, 2),
        delta_amount=round(delta, 2),
        delta_percentage=round(delta_pct, 2),
        fulfillment_gap_pct=fulfillment_gap_pct,
        sla_priority=sla_priority,
        sla_deadline=None,         # grandfathered — no producer
        affected_lines=affected_lines,
    )


def _sla_priority_for(record: ExceptionRecord) -> str:
    """Map shadow verdict + lifecycle to a coarse SLA priority label.

    Pure presentation mapping — no business-rule authority. The UI
    accepts any string here per Guardrail #2 (visual-mapping
    function with default fallback).
    """
    verdict = (record.shadow_verdict or "").upper()
    if verdict == "RED":
        return "P1"
    if verdict == "YELLOW":
        return "P2"
    if verdict == "GREEN":
        return "P3"
    return "P4"


# ---------------------------------------------------------------------------
# Narrative — root_cause + recommendation
# ---------------------------------------------------------------------------

def compose_narrative(
    record: ExceptionRecord,
    trace_data: Optional[Dict[str, Any]],
) -> Tuple[Optional[str], Optional[str]]:
    """Extract order-level root_cause + recommendation prose.

    Sources, in priority order:
      1. `record.resolution_data` keys (recipe-supplied narrative)
      2. `trace_data.narrative` for the root cause
      3. `trace_data.resolution_steps[0]` for the recommendation
    Returns (None, None) when nothing is available — the UI hides
    each block individually rather than showing an empty bar.
    """
    rd = record.resolution_data or {}
    root_cause = _first_string(rd, ("root_cause", "root_cause_summary"))
    recommendation = _first_string(
        rd, ("recommendation", "recommended_action", "summary")
    )

    if trace_data:
        if root_cause is None:
            narrative = trace_data.get("narrative")
            if isinstance(narrative, str) and narrative.strip():
                # Take the first paragraph as the root-cause prose;
                # the rest is the long-form Layer-2 narrative the UI
                # already surfaces in DiagnosticsSection.
                root_cause = narrative.strip().split("\n\n", 1)[0]
        if recommendation is None:
            steps = trace_data.get("resolution_steps")
            if isinstance(steps, list) and steps:
                first = steps[0]
                if isinstance(first, str) and first.strip():
                    recommendation = first.strip()

    return root_cause, recommendation


def _first_string(d: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None
