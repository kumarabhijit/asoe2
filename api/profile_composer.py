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

from api.schemas import (
    ChangeAnalysis,
    DraftReply,
    DraftReplyEdit,
    Edi850Document,
    EntitiesAnalysisData,
    EntityProfile,
    ExtractedEntity,
    ImpactMetrics,
    KnowledgeGraphPayload,
    OrderEntryExtraction,
    SapDataAnalysisData,
)
from api.store import ChildCase
from api.users import get_account


# ---------------------------------------------------------------------------
# Entity profile
# ---------------------------------------------------------------------------

def compose_entity_profile(record: ChildCase) -> Optional[EntityProfile]:
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
# Customer Inbox sections (ADR-042 Phase 2) — cross-cutting projections
# from enrichment_context. SOLE source of truth (no recipe/UI synthesis);
# return None when the backing gateway context is absent so the UI
# structurally omits the tab (Guardrail #6 — no partial-truth). Preview-only
# until the intake-extraction / SAP-read gateways populate enrichment_context.
# ---------------------------------------------------------------------------

def compose_entities_analysis(
    record: ChildCase,
) -> Optional[EntitiesAnalysisData]:
    """Project the AI-extracted entities from
    `enrichment_context["inbox_entities"]` (shape: ``{"extracted": [...]}``).
    None when absent or empty."""
    ctx = record.enrichment_context.get("inbox_entities")
    raw = ctx.get("extracted") if isinstance(ctx, dict) else None
    if not isinstance(raw, list) or not raw:
        return None
    try:
        entities = [ExtractedEntity(**e) for e in raw if isinstance(e, dict)]
    except (TypeError, ValueError):
        return None
    if not entities:
        return None
    return EntitiesAnalysisData(extracted=entities)


def compose_sap_data_analysis(
    record: ChildCase,
) -> Optional[SapDataAnalysisData]:
    """Project live SAP context from `enrichment_context["sap_data"]`. Requires
    `system` + `validation_status` (the audit-bearing anchors); None otherwise."""
    ctx = record.enrichment_context.get("sap_data")
    if not isinstance(ctx, dict):
        return None
    if not ctx.get("system") or not ctx.get("validation_status"):
        return None
    try:
        return SapDataAnalysisData(
            system=ctx["system"],
            validation_status=ctx["validation_status"],
            order_value_usd=ctx.get("order_value_usd"),
            sap_doc_number=ctx.get("sap_doc_number"),
        )
    except (TypeError, ValueError):
        return None


def compose_order_entry_extraction(
    record: ChildCase,
) -> Optional[OrderEntryExtraction]:
    """Project the extracted order form from
    `enrichment_context["order_entry_extraction"]`. None when absent or
    malformed (preview-only until the extraction gateway lands)."""
    ctx = record.enrichment_context.get("order_entry_extraction")
    if not isinstance(ctx, dict) or not ctx:
        return None
    try:
        return OrderEntryExtraction(**ctx)
    except (TypeError, ValueError):
        return None


def compose_edi_850_document(
    record: ChildCase,
) -> Optional[Edi850Document]:
    """Project the deterministically built EDI 850 from
    `enrichment_context["edi_850"]` (the `edi_850` builder producer's read).
    None when absent or malformed (preview-only until the producer is wired).
    The builder owns construction (`gateways/edi850.py`); the composer only
    projects — no synthesis here (Guardrail #6)."""
    ctx = record.enrichment_context.get("edi_850")
    if not isinstance(ctx, dict) or not ctx:
        return None
    try:
        return Edi850Document(**ctx)
    except (TypeError, ValueError):
        return None


def compose_change_analysis(
    record: ChildCase,
) -> Optional[ChangeAnalysis]:
    """Project the deterministic Change Analysis from
    `enrichment_context["change_analysis"]` (the recipe-homed evaluator's read).
    None when absent or malformed (preview-only until the producer is wired).
    The evaluator owns the logic (`recipes/ChangeAnalysisRecipe.py`); the
    composer only projects — no synthesis here (Guardrail #6)."""
    ctx = record.enrichment_context.get("change_analysis")
    if not isinstance(ctx, dict) or not ctx:
        return None
    try:
        return ChangeAnalysis(**ctx)
    except (TypeError, ValueError):
        return None


def compose_knowledge_graph(
    record: ChildCase,
) -> Optional[KnowledgeGraphPayload]:
    """Project the derived knowledge graph from
    `enrichment_context["knowledge_graph"]` (the builder producer's read). None
    when absent, malformed, or empty (no projectable entities → tab omitted;
    deferrable per ADR-042 §5b). The builder owns construction
    (`gateways/knowledge_graph.py`); the composer only projects (Guardrail #6)."""
    ctx = record.enrichment_context.get("knowledge_graph")
    if not isinstance(ctx, dict) or not ctx.get("nodes"):
        return None
    try:
        return KnowledgeGraphPayload(**ctx)
    except (TypeError, ValueError):
        return None


def compose_draft_reply(record: ChildCase) -> Optional[DraftReply]:
    """Project the current AI reply draft from `resolution_data["reply_draft"]`
    (the ReplyDraftRecipe output a DRAFT_REPLY disposition persisted). None when
    no draft exists. Flattens the nested `draft` block into the typed contract;
    the recipe owns generation — the composer only projects (Guardrail #6)."""
    rd = record.resolution_data or {}
    ctx = rd.get("reply_draft")
    if not isinstance(ctx, dict) or not ctx.get("status"):
        return None
    draft = ctx.get("draft") if isinstance(ctx.get("draft"), dict) else {}
    try:
        edits = [
            DraftReplyEdit(
                field=str(e.get("field", "")),
                before=_opt_str(e.get("before")),
                after=_opt_str(e.get("after")),
            )
            for e in (ctx.get("edits_applied") or [])
            if isinstance(e, dict)
        ]
        return DraftReply(
            status=ctx["status"],
            reason=_opt_str(ctx.get("reason")),
            template_name=_opt_str(draft.get("template_name")),
            recipient=_opt_str(draft.get("recipient")),
            subject=_opt_str(draft.get("subject")),
            body=_opt_str(draft.get("body")),
            edits_applied=edits,
            drafted_by=_opt_str(ctx.get("drafted_by")),
            drafted_at=_opt_str(ctx.get("drafted_at")),
        )
    except (TypeError, ValueError):
        return None


def _opt_str(value: Any) -> Optional[str]:
    return None if value is None else str(value)


# ---------------------------------------------------------------------------
# Impact metrics
# ---------------------------------------------------------------------------

def compose_impact_metrics(record: ChildCase) -> Optional[ImpactMetrics]:
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


def _sla_priority_for(record: ChildCase) -> str:
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
    record: ChildCase,
    trace_data: Optional[Dict[str, Any]],
) -> Tuple[Optional[str], Optional[str]]:
    """Extract order-level root_cause + recommendation prose.

    Sources, in priority order:
      1. ``record.resolution_data`` keys (recipe-supplied narrative).
      2. ``trace_data.narrative`` for the root cause.
      3. ``trace_data.resolution_steps[0]`` for the recommendation.
      4. **Deterministic projection from recipe output** (per-intent
         templates that read already-decided fields like
         ``composite_score``, ``classification``, ``applied_condition``,
         and the inbound event's pricing / credit fields). Each
         template requires its underlying data to be present — when
         the data is missing, the template returns ``None`` and the UI
         keeps the section structurally omitted.

    The fourth tier is projection, not fabrication: the prose
    summarises what the recipe already decided. No new business logic
    or thresholds are introduced. This is the same architectural
    pattern the audit-bearing field projections use — the composer
    reads recipe / event / shadow output and types it into the UI
    contract (CLAUDE.md Guardrail #6).

    Returns (None, None) only when neither the recipe nor the
    deterministic projection produces text — the UI then hides each
    block individually rather than showing an empty bar.
    """
    rd = record.resolution_data or {}
    root_cause = _first_string(rd, ("root_cause", "root_cause_summary"))
    recommendation = _first_string(
        rd, ("recommendation", "recommended_action", "summary")
    )

    if trace_data:
        if root_cause is None:
            # Tier 2 — trace `narrative` (long-form). Take the first
            # paragraph; the rest is the long-form Layer-2 narrative
            # the UI already surfaces in DiagnosticsSection.
            narrative = trace_data.get("narrative")
            if isinstance(narrative, str) and narrative.strip():
                root_cause = narrative.strip().split("\n\n", 1)[0]
            else:
                # Tier 2b — trace `explanation` (short-form, present on
                # every halt path including AUDIT_CONTEXT_MISSING and
                # FAIL_TO_HUMAN where `narrative` is rarely written).
                # Same first-paragraph rule so the audit-gap detail
                # stays in the long-form Diagnostics view.
                explanation = trace_data.get("explanation")
                if isinstance(explanation, str) and explanation.strip():
                    root_cause = explanation.strip().split("\n\n", 1)[0]
        if recommendation is None:
            steps = trace_data.get("resolution_steps")
            if isinstance(steps, list) and steps:
                first = steps[0]
                if isinstance(first, str) and first.strip():
                    recommendation = first.strip()

    # Tier 4 — deterministic projection from recipe output. Runs only
    # for fields that are still ``None`` after the previous tiers, so
    # an explicit recipe-supplied string always wins.
    if root_cause is None:
        root_cause = _synthesise_root_cause(record)
    if recommendation is None:
        recommendation = _synthesise_recommendation(record)

    return root_cause, recommendation


# ---------------------------------------------------------------------------
# Deterministic prose templates (Tier 4 in compose_narrative).
#
# Each helper reads already-computed fields from `record.resolution_data`
# / `record.original_event` and projects them into a one-paragraph
# narrative. NEVER introduces new thresholds, decisions, or business
# logic — that lives in recipes. NEVER falls back to a generic phrase
# when the underlying data is absent — returns None so the UI omits
# the section.
# ---------------------------------------------------------------------------


def _synthesise_root_cause(record: ChildCase) -> Optional[str]:
    intent = (record.intent or "").upper() if record.intent else ""
    rd = record.resolution_data or {}
    event = record.original_event or {}

    if intent == "CONTRACTUAL_CORRECTION":
        po_price = _as_float(event.get("po_price"))
        erp_price = _as_float(event.get("sap_base_price"))
        if po_price is not None and erp_price not in (None, 0.0):
            delta_pct = (po_price - erp_price) / erp_price * 100.0
            return (
                f"PO price ${po_price:.2f} differs from ERP master "
                f"${erp_price:.2f} by {delta_pct:+.1f}%."
            )
        return None

    if intent == "CREDIT_BLOCK":
        limit = _as_float(event.get("credit_limit"))
        exposure = _as_float(event.get("current_exposure"))
        if limit is not None and exposure is not None:
            delta = exposure - limit
            if delta > 0:
                return (
                    f"Current exposure ${exposure:,.0f} exceeds credit "
                    f"limit ${limit:,.0f} by ${delta:,.0f}."
                )
            return (
                f"Credit hold raised on order despite exposure "
                f"${exposure:,.0f} within limit ${limit:,.0f}."
            )
        return None

    if intent == "DUPLICATE_PO":
        score = _as_float(rd.get("composite_score"))
        classification = rd.get("classification")
        meta = event.get("metadata", {}) if isinstance(event, dict) else {}
        matched = (
            rd.get("matched_po_id")
            or (meta.get("matched_po_id") if isinstance(meta, dict) else None)
        )
        if score is not None and matched:
            label = classification or "match"
            return (
                f"Inbound PO matches prior PO {matched} with composite "
                f"score {score:.2f} ({label})."
            )
        return None

    if intent == "MASS_PRICING_ERROR":
        line_count = event.get("line_count") if isinstance(event, dict) else None
        if isinstance(line_count, (int, float)) and line_count > 0:
            return (
                f"Mass-pricing error spanning {int(line_count)} line item"
                f"{'' if line_count == 1 else 's'} on the inbound order."
            )
        return None

    # Universal fallback — applies to every intent (BACK_ORDER,
    # DELIVERY_DELAY, EDI_MISMATCH, OVER_MAX, MIN_ORDER_QTY,
    # PALLET_CONFIG, PRICE_HOLD_RELEASE, etc.) when the per-intent
    # template above didn't fire. Composes from the
    # already-decided shadow verdict + final status, which are the
    # closest thing to a "what happened" summary that is universally
    # available without inventing data. Returns None when neither is
    # populated (the recipe / orchestrator never reached a verdict).
    verdict = (record.shadow_verdict or "").upper()
    final = (record.final_status or "").upper()
    intent_label = intent.replace("_", " ").title() if intent else None

    if final == "AUDIT_CONTEXT_MISSING" and intent_label:
        return (
            f"{intent_label} record reached a registry-enforced audit gap — "
            f"required evidence fields were missing from the recipe / "
            f"gateway output, so the record cannot be presented for "
            f"operator action."
        )
    if verdict and final and intent_label:
        return (
            f"{intent_label} event evaluated by Compliance Shadow ({verdict}); "
            f"orchestration concluded with terminal status {final}."
        )
    if final and intent_label:
        return f"{intent_label} event concluded with terminal status {final}."

    return None


def _synthesise_recommendation(record: ChildCase) -> Optional[str]:
    rd = record.resolution_data or {}
    intent = (record.intent or "").upper() if record.intent else ""

    # Most recipes already write a ``recommended_action`` token (e.g.
    # ``BLOCK_AND_NOTIFY``, ``ALLOW_BOTH``). Surface it as a sentence
    # rather than an opaque enum so the UI's Recommendation block
    # reads as prose.
    action = rd.get("recommended_action")
    if isinstance(action, str) and action.strip():
        humanised = action.replace("_", " ").lower()
        if intent == "DUPLICATE_PO":
            return f"Recipe recommends: {humanised}."
        if intent == "CREDIT_BLOCK":
            return f"Route to Finance review with action: {humanised}."
        return f"Recommended action: {humanised}."

    # CONTRACTUAL_CORRECTION recipe writes ``applied_condition`` +
    # ``new_net_price`` instead of ``recommended_action``.
    cond = rd.get("applied_condition")
    new_price = _as_float(rd.get("new_net_price"))
    if isinstance(cond, str) and cond.strip() and new_price is not None:
        return (
            f"Apply SAP condition {cond} at new net price "
            f"${new_price:.2f}."
        )

    # Final status is the last-resort signal — when the recipe halted
    # without writing a recommendation, surface the terminal state
    # so the operator at least sees what to do next.
    final = (record.final_status or "").upper()
    if final == "COMPLETE":
        return "Resolution applied — no further action required."
    if final == "COMPLETE_WITH_CHILDREN":
        return "Workflow steps applied — follow up on child resolutions."
    if final == "MANUAL_REVIEW_REQUIRED":
        return "Route to manual review per Compliance Shadow verdict."
    if final == "BLOCKED":
        return "Hold the order until policy permits release."
    if final == "REJECTED":
        return "Reject the inbound order and notify the buyer."
    if final == "FAIL_TO_HUMAN":
        return "Escalate to a human operator — deterministic path unavailable."
    if final == "AUDIT_CONTEXT_MISSING":
        # The diagnosis surface enumerates the missing audit-bearing
        # fields; the operator-facing action is to wire the missing
        # gateway evidence and re-run, OR apply a registry
        # grandfather clause per compliance/audit_bearing_registry.yaml.
        return (
            "Wire missing gateway evidence (or apply a compliance "
            "grandfather clause) and re-run analysis."
        )

    return None


def _as_float(value: Any) -> Optional[float]:
    """Coerce a recipe / event field to float; return None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_string(d: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None
