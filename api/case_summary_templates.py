"""ADR-041 P3e §3.3, Phase 0b T2b — per-intent CaseSummary template registry.

Pure projection layer. The asoe2 `compute_case_summary` projection
(api/case_summary.py) calls into this module to populate
`top_line_sku_code`, `top_line_sku_title`, and `problem_one_liner`
per-intent on the wire shape that drives the `/cases` V2 queue
row. Without these templates the row's line 3 (subject) is empty
and the operator has to click in to learn the case shape.

## What ships in this scaffold (Phase 0a follow-on)

A registry keyed on intent, dispatched by `render_template`. Two
intents have working implementations (DUPLICATE_PO, MANUAL_ORDER_INTAKE)
because their `resolution_data` + `enrichment_context` keys are
stable across the recipe surface. The rest carry `# TODO(recipe-team)`
markers with the verbatim spec from the 2026-05-28 Recipe SME panel
response.

The hand-off contract is intentionally narrow:

  * Each template is a pure function
    `(record: ChildCase, case: OrderCase) -> RenderedTemplate | None`.
  * `None` means "no honest one-liner available with current
    recipe output" — the dispatcher returns null fields rather
    than synthesising.
  * Templates read ONLY from `record.resolution_data`,
    `record.enrichment_context`, and `case.*`. No I/O, no gateway
    fetches, no event-metadata blending (Guardrail #6).

## Known recipe-output gaps (grandfather-clause candidates)

  * PRICE_HOLD — `PriceHoldAnalysisData` carries no `sku` / `qty`;
    template returns None until the recipe extends the output.
  * EMAIL_COMPLAINT (pure intake) — no quantity fields in
    `EmailOrderEntryAnalysisData`; template returns None.
  * EDI_MISMATCH — sub-type drives meaning; no honest single-line
    summary (Recipe SME §3 says hide the column).
  * PALLET — labor/freight waste, not revenue.

These four DO NOT get filled in; their stub returns None. The
correct path for closing each gap is Verdict 2026-04-22 Pillar 1
(extend the recipe's captured context) plus a registry entry under
`compliance/audit_bearing_registry.yaml::grandfather_clauses`.

## Phase 0b deliverables

T2b ships this file with the registry + DUPLICATE_PO and
MANUAL_ORDER_INTAKE working. T2c (verdict color gates), T2d
(multi-currency), T2e (per-line breakdown) are tracked in
`docs/specs/case-summary-projection.md`.

For the rest of the intents, the Recipe team owns:
  1. Confirm field names on the relevant `*AnalysisData` are
     produced by the recipe today (or schedule the recipe
     extension).
  2. Replace the `# TODO(recipe-team)` stub with the working
     template per the spec.
  3. Add a unit test mirroring the Recipe SME's example output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


@dataclass(frozen=True)
class RenderedTemplate:
    """The three template-rendered fields. Each is independently
    nullable — a template MAY return SKU but no one-liner (or vice
    versa) when only one is computable from the recipe output.
    """

    sku_code: Optional[str] = None
    sku_title: Optional[str] = None
    one_liner: Optional[str] = None


# Template function signature. `record` is the primary ChildCase
# (picked by `case_summary._pick_primary_record`); `case` is the
# parent OrderCase. Returns None when the recipe output is too
# sparse to produce an honest summary.
TemplateFn = Callable[[Any, Any], Optional[RenderedTemplate]]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _with_more_suffix(title: Optional[str], plan_length: int) -> Optional[str]:
    """ADR-041 P3e Phase 0b T2e — multi-line indicator.

    Append " +N more" to the title when the recipe's per-line
    plan (`trim_plan` for OVER_MAX, `round_up_plan` for MOQ,
    etc.) covers more than one SKU. The displayed SKU is the
    head entry; the suffix signals "the row represents more
    than just this line."

    Returns the title unchanged when:
      * plan_length <= 1 (single-line — no suffix needed)
      * title is None AND plan_length <= 1 (nothing to render)

    When title is None but plan_length > 1, returns the suffix
    alone so the line-count indicator still surfaces.
    """
    extra = plan_length - 1
    if extra <= 0:
        return title
    suffix = f"+{extra} more"
    if title:
        return f"{title} (+{extra} more)"
    return suffix


# ---------------------------------------------------------------------------
# Per-intent template implementations
# ---------------------------------------------------------------------------


def _duplicate_po_template(record, _case) -> Optional[RenderedTemplate]:
    """Recipe SME spec:
       `PO {duplicate_order.po_number} — duplicate of
        {original_order.po_number} ({days_between}d apart,
        {confidence}% match)`

    Field source: `record.resolution_data["duplicate_detection"]`
    (composed by DuplicatePORecipe). All three fields required; if
    any is missing the recipe is mid-classification — return None
    so the row collapses to status chips only.
    """
    rd = getattr(record, "resolution_data", None) or {}
    detection = rd.get("duplicate_detection") or {}
    dup = detection.get("duplicate_order") or {}
    orig = detection.get("original_order") or {}
    dup_po = dup.get("po_number")
    orig_po = orig.get("po_number")
    days = detection.get("days_between")
    confidence = detection.get("confidence")
    if dup_po is None or orig_po is None or days is None or confidence is None:
        return None
    one_liner = (
        f"PO {dup_po} — duplicate of {orig_po} "
        f"({int(days)}d apart, {int(round(float(confidence) * 100))}% match)"
    )
    return RenderedTemplate(one_liner=one_liner)


def _manual_order_intake_template(record, _case) -> Optional[RenderedTemplate]:
    """Email-arrived order shapes (NEW_ORDER, ORDER_CHANGE,
    INQUIRY, COMPLAINT, OTHER) — the only honest summary we can
    build at intake time is the buyer's stated email subject + a
    coarse type tag.

    Field source: `record.enrichment_context["email_source"]`
    (`EmailSourceAnalysisData`). Returns None when the email-source
    enrichment hasn't landed yet (preprod fallback path).
    """
    enrichment = getattr(record, "enrichment_context", None) or {}
    email = enrichment.get("email_source") or {}
    subject = email.get("subject")
    classification = email.get("classification") or "OTHER"
    if not subject:
        return None
    # Truncate the subject — the row's line 3 already truncates at
    # the DOM level, but a 200-char subject leaves no room for the
    # classification tag visually. Trim to 80 chars + ellipsis.
    if len(subject) > 80:
        subject = subject[:77] + "…"
    return RenderedTemplate(
        one_liner=f"{classification.replace('_', ' ').title()}: {subject}",
    )


def _grandfathered_no_template(_record, _case) -> Optional[RenderedTemplate]:
    """Explicit no-op for intents whose recipe output cannot today
    produce an honest one-liner (PRICE_HOLD, EDI_MISMATCH, PALLET,
    EMAIL_COMPLAINT-intake). Returns None — the projection then
    ships null fields on these intents, which is the
    grandfather-clause behaviour the Recipe SME panel signed off.

    Distinct symbol (not just absence-from-the-registry) so the
    `compliance/audit_bearing_registry.yaml::grandfather_clauses`
    parity test can grep for the deliberate no-ops vs. accidental
    omissions.
    """
    return None


# TODO(recipe-team): Phase 0b T2b — implement the remaining
# templates per the verbatim spec below. Each `# TODO` block names
# the Recipe SME's template string + the field source. Implement
# in this order (most operator-visible first):
#   1. PRICE_DISCREPANCY — largest queue volume on demo tenants
#   2. BACK_ORDER (a.k.a. ORDER_COMPARISON) — second-largest
#   3. CREDIT_BLOCK — high-impact, low-volume
#   4. CONTRACTUAL_CORRECTION — overlaps with PRICE_DISCREPANCY
#   5. MASS_PRICING_ERROR — bulk variant
#   6. OVER_MAX, MOQ_UPLIFT, DELIVERY_DELAY


def _price_discrepancy_template(record, _case) -> Optional[RenderedTemplate]:
    """Recipe SME spec:
       `{sku} — {material_desc} (PO ${po_unit_price}/{uom} vs ERP
        ${erp_unit_price}; {variance_pct}% delta, ${total_at_risk}
        at risk)`

    Implementation: dispatches to the existing `adapt_price`
    adapter so the field projection matches the OrderAnalysis
    composer byte-for-byte. The adapter returns None when any
    audit-bearing gateway field is absent — we treat the same way
    (Structurally Omitted on the queue row). `material_desc` is
    contextual; we render with or without it.
    """
    # Lazy import — keeps the module's import graph independent
    # of the heavy analysis_adapters surface for the dispatcher
    # cases that don't need it.
    from api.analysis_adapters import adapt_price

    try:
        data = adapt_price(record)
    except Exception:
        # Recipe-output shape mismatch in production — never let
        # template rendering crash the route hot path.
        return None
    if data is None:
        return None
    sku = data.sku
    variance_pct = data.variance_pct
    at_risk = data.total_at_risk
    po = data.po_unit_price
    erp = data.erp_unit_price
    uom = data.uom
    desc = data.material_desc
    title = desc if desc else None
    one_liner = (
        f"PO ${po:.2f}/{uom} vs ERP ${erp:.2f} — "
        f"{variance_pct:.1f}% delta, ${at_risk:,.2f} at risk"
    )
    return RenderedTemplate(sku_code=sku, sku_title=title, one_liner=one_liner)


def _back_order_template(record, _case) -> Optional[RenderedTemplate]:
    """Recipe SME spec:
       `{sku} — short stock: {available_qty}/{ordered_qty} {uom}
        ({gap_pct}% gap, ATP {atp_date})`

    Dispatches to `adapt_back_order` for the audit-bearing
    quantity + ATP fields; SKU pulls from
    `record.original_event["sku"]` (the recipe input) — the
    BackOrderAnalysisData schema doesn't carry sku, but the
    one-liner is more readable with it. Renders without the
    sku prefix when original_event is missing or carries no sku.
    """
    from api.analysis_adapters import adapt_back_order

    try:
        data = adapt_back_order(record)
    except Exception:
        return None
    if data is None:
        return None
    event = getattr(record, "original_event", None) or {}
    sku = event.get("sku") if isinstance(event, dict) else None
    one_liner = (
        f"Short stock: {data.available_qty:g}/{data.ordered_qty:g} "
        f"{data.uom} ({data.gap_pct:.1f}% gap, ATP {data.atp_date})"
    )
    return RenderedTemplate(sku_code=sku, sku_title=None, one_liner=one_liner)


def _credit_block_template(record, _case) -> Optional[RenderedTemplate]:
    """Recipe SME spec:
       `Credit limit breached: ${current_exposure} exposure over
        ${credit_limit} limit`

    Reads from `record.enrichment_context["customer_master_context"]`
    when the SAP customer-master gateway has wired (today: only
    in sandbox). Field-presence guard returns None when either
    field is absent — the gateway gap is documented under the
    credit_block_customer_master grandfather candidate.

    No adapter exists yet (CreditHoldReleaseRecipe doesn't
    project structured analysis the way PriceAdjustmentRecipe
    does); when the adapter lands this template flips to the
    same dispatch-to-adapter pattern as the others.
    """
    enrichment = getattr(record, "enrichment_context", None) or {}
    cm_ctx = enrichment.get("customer_master_context") or {}
    if not isinstance(cm_ctx, dict):
        return None
    credit_limit = cm_ctx.get("credit_limit")
    current_exposure = cm_ctx.get("current_exposure")
    try:
        limit_f = float(credit_limit) if credit_limit is not None else None
        exposure_f = (
            float(current_exposure) if current_exposure is not None else None
        )
    except (TypeError, ValueError):
        return None
    if limit_f is None or exposure_f is None:
        return None
    one_liner = (
        f"Credit limit breached: ${exposure_f:,.0f} exposure over "
        f"${limit_f:,.0f} limit"
    )
    return RenderedTemplate(one_liner=one_liner)


def _contractual_correction_template(record, case) -> Optional[RenderedTemplate]:
    """Recipe SME spec:
       `{sku} — Contract price ${erp_unit_price} vs PO
        ${po_unit_price} — {variance_pct}% variance`

    CONTRACTUAL_CORRECTION and PRICE_DISCREPANCY share the
    PriceAdjustmentRecipe surface (see recipes/EdiMismatchRecipe.py
    comment at line 105 — both intents route through that
    recipe). Delegates to _price_discrepancy_template for the
    same field projection; the visible vocabulary on the row is
    intent-agnostic since the variance + at-risk values are the
    operator's decision signal regardless of the intent label.
    """
    return _price_discrepancy_template(record, case)


def _mass_pricing_error_template(record, _case) -> Optional[RenderedTemplate]:
    """Recipe SME spec:
       `Bulk price drift across {line_count} lines (avg
        {avg_variance_pct}% variance)`

    MASS_PRICING_ERROR routes to FAIL_TO_HUMAN upstream
    (recipes/DuplicatePORecipe.py:36 — no recipe handles this
    intent's resolution today; operators get the case as
    manual-review). The recipe-input event metadata is what we
    have: `line_count` is on the OrderEvent shape, and we
    aggregate the variance signal from the line_items if
    populated.

    Hides sku_code on the row by construction — multi-SKU intent.
    Returns None when neither line_count nor a usable variance is
    available.
    """
    event = getattr(record, "original_event", None) or {}
    if not isinstance(event, dict):
        return None
    line_count = event.get("line_count")
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    # Variance: prefer the recipe-input avg, else compute from
    # line_items when available. Either signal alone is enough to
    # render an honest one-liner; both absent → None.
    avg_variance_pct = metadata.get("avg_variance_pct")
    if avg_variance_pct is None:
        items = event.get("line_items") if isinstance(event.get("line_items"), list) else []
        deltas = []
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                pp = float(it.get("po_price") or 0)
                sp = float(it.get("sap_base_price") or 0)
                if sp > 0:
                    deltas.append(abs(pp - sp) / sp * 100.0)
            except (TypeError, ValueError):
                continue
        avg_variance_pct = (sum(deltas) / len(deltas)) if deltas else None
    if line_count is None and avg_variance_pct is None:
        return None
    parts = []
    if line_count is not None:
        try:
            n = int(line_count)
            parts.append(f"Bulk price drift across {n} lines")
        except (TypeError, ValueError):
            pass
    if avg_variance_pct is not None:
        try:
            parts.append(f"avg {float(avg_variance_pct):.1f}% variance")
        except (TypeError, ValueError):
            pass
    if not parts:
        return None
    one_liner = (
        f"{parts[0]} ({parts[1]})" if len(parts) == 2 else parts[0]
    )
    return RenderedTemplate(one_liner=one_liner)


def _over_max_template(record, _case) -> Optional[RenderedTemplate]:
    """Recipe SME spec:
       `{trim_plan[0].sku} — over max by {excess_qty} {uom}
        ({exceedance_pct}% over)`

    Dispatches to `adapt_overmax`. SKU + title come from
    `trim_plan[0]` — the recipe pre-sorts by value, so the head
    entry is the line driving the operator's decision. Renders
    without the sku prefix when trim_plan is empty.

    Phase 0b T2e — multi-line cases append " +N more" to the
    sku_title so the operator sees that the case spans more than
    the displayed top line. Driven off len(trim_plan), not a
    blind line count, so the suffix accurately counts the lines
    the recipe flagged for trim.
    """
    from api.analysis_adapters import adapt_overmax

    try:
        data = adapt_overmax(record)
    except Exception:
        return None
    if data is None:
        return None
    sku: Optional[str] = None
    title: Optional[str] = None
    if data.trim_plan:
        head = data.trim_plan[0]
        sku = head.sku or None
        # TrimPlanLine carries description as a contextual field.
        desc = getattr(head, "description", None)
        title = desc if desc else None
        title = _with_more_suffix(title, len(data.trim_plan))
    one_liner = (
        f"Over max by {data.excess_qty:g} {data.uom} "
        f"({data.exceedance_pct:.1f}% over)"
    )
    return RenderedTemplate(sku_code=sku, sku_title=title, one_liner=one_liner)


def _moq_uplift_template(record, _case) -> Optional[RenderedTemplate]:
    """Recipe SME spec:
       `{sku} — below MOQ by {shortfall_qty} {uom} (ordered
        {ordered_qty} vs {moq_qty} required)`

    Dispatches to `adapt_moq`. SKU + contextual description
    project directly from the analysis data.

    Phase 0b T2e — when the recipe's round_up_plan covers more
    than one line, append " +N more" to the sku_title so the
    operator knows the row represents multiple lines.
    """
    from api.analysis_adapters import adapt_moq

    try:
        data = adapt_moq(record)
    except Exception:
        return None
    if data is None:
        return None
    title = data.description if data.description else None
    title = _with_more_suffix(title, len(data.round_up_plan))
    one_liner = (
        f"Below MOQ by {data.shortfall_qty:g} {data.uom} "
        f"(ordered {data.ordered_qty:g} vs {data.moq_qty:g} required)"
    )
    return RenderedTemplate(
        sku_code=data.sku, sku_title=title, one_liner=one_liner,
    )


def _delivery_delay_template(record, _case) -> Optional[RenderedTemplate]:
    """Recipe SME spec:
       `{affected_lines} line(s) — {days_late}d late
        ({delay_category}, ETA {projected_eta})`

    Dispatches to `adapt_delivery_delay`. DeliveryDelayAnalysisData
    doesn't carry a SKU (multi-line by construction), so sku_code
    + sku_title stay None — line 3 on the queue row collapses to
    the one-liner only, which matches the multi-line intent
    shape Recipe SME §3 documented.

    `dollar_impact` for this intent is null at the projection
    layer until the gateway completes (2026-07-21 target).
    """
    from api.analysis_adapters import adapt_delivery_delay

    try:
        data = adapt_delivery_delay(record)
    except Exception:
        return None
    if data is None:
        return None
    line_word = "line" if data.affected_lines == 1 else "lines"
    one_liner = (
        f"{data.affected_lines} {line_word} — {data.days_late}d late "
        f"({data.delay_category}, ETA {data.projected_eta})"
    )
    return RenderedTemplate(one_liner=one_liner)


def _change_analysis_template(record, _case) -> Optional[RenderedTemplate]:
    """Recipe SME spec:
       `{change_items[0].field}: {from_value} → {to_value} (rec:
        {decision.recommended_action}, ${revenue_impact_usd})`

    Dispatches to `compose_change_analysis` so we project from
    `enrichment_context["change_analysis"]` via the typed
    ChangeAnalysis Pydantic shape. Renders the first change item
    (recipe pre-sorts by revenue-impact desc). Renders without
    the revenue-impact suffix when the field is absent (it's
    Optional on ChangeDecision).
    """
    from api.profile_composer import compose_change_analysis

    try:
        data = compose_change_analysis(record)
    except Exception:
        return None
    if data is None:
        return None
    items = data.evaluation.change_items
    if not items:
        return None
    head = items[0]
    field = head.field
    from_v = head.from_value if head.from_value is not None else "—"
    to_v = head.to_value if head.to_value is not None else "—"
    action = data.decision.recommended_action
    revenue = data.decision.revenue_impact_usd
    suffix = (
        f", ${revenue:,.2f}" if isinstance(revenue, (int, float)) else ""
    )
    one_liner = (
        f"{field}: {from_v} → {to_v} (rec: {action}{suffix})"
    )
    return RenderedTemplate(one_liner=one_liner)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


# Intent name → template function. Intents NOT in this registry
# get the all-null behaviour (Recipe SME's hide-the-row state).
INTENT_TEMPLATES: Dict[str, TemplateFn] = {
    # Working implementations:
    "DUPLICATE_PO":            _duplicate_po_template,
    "MANUAL_ORDER_INTAKE":     _manual_order_intake_template,
    # Explicit grandfather-clause no-ops (distinct from "missing"
    # so the registry-coverage check can tell them apart). Keyed by the
    # runtime Intent enum value, not the SME panel's working label
    # (PRICE_HOLD_RELEASE not PRICE_HOLD; PALLET_CONFIG not PALLET), so the
    # lookup actually matches the record's intent.
    "PRICE_HOLD_RELEASE":      _grandfathered_no_template,
    "EDI_MISMATCH":            _grandfathered_no_template,
    "PALLET_CONFIG":           _grandfathered_no_template,
    "EMAIL_COMPLAINT":         _grandfathered_no_template,
    # Recipe-team TODOs:
    "PRICE_DISCREPANCY":       _price_discrepancy_template,
    "BACK_ORDER":              _back_order_template,
    "CREDIT_BLOCK":            _credit_block_template,
    "CONTRACTUAL_CORRECTION":  _contractual_correction_template,
    "MASS_PRICING_ERROR":      _mass_pricing_error_template,
    "OVER_MAX":                _over_max_template,
    # Keyed by the runtime Intent enum value (contracts/models.py::Intent),
    # not the Recipe-SME panel's working label "MOQ_UPLIFT" — the record's
    # intent is "MIN_ORDER_QTY", so the panel label never matched and the
    # template (which exists) was unreachable. Wire it to the enum.
    "MIN_ORDER_QTY":           _moq_uplift_template,
    "DELIVERY_DELAY":          _delivery_delay_template,
    "CHANGE_ANALYSIS":         _change_analysis_template,
}


def render_template(record, case) -> RenderedTemplate:
    """Dispatch to the registered template for the record's intent.
    Returns an empty `RenderedTemplate()` (all fields None) when:

      * the record is None (no primary record on this case yet);
      * the intent is absent from the registry (unrecognised /
        new intent the Recipe team hasn't added yet);
      * the template itself returns None (recipe output too sparse).

    The "all None" return is the dispatch-side equivalent of the
    EvidenceBlock Structurally Omitted contract on the UI side.
    """
    if record is None:
        return RenderedTemplate()
    intent = getattr(record, "intent", None)
    if intent is None:
        return RenderedTemplate()
    template = INTENT_TEMPLATES.get(intent)
    if template is None:
        return RenderedTemplate()
    result = template(record, case)
    return result if result is not None else RenderedTemplate()


_HEADLINE_MAX = 110


def reason_headline_fallback(record) -> Optional[str]:
    """Per-record one-liner fallback when the intent renders no template.

    The grandfather-clause intents (PRICE_HOLD_RELEASE / EDI_MISMATCH /
    PALLET_CONFIG) have no template, so the queue row + Situation headline were
    blank. Fall back to the recipe-supplied ``resolution_data["reason"]`` — the
    Recipe team owns this content, so this is NOT a UI-side synthesis from event
    metadata (the constraint ``test_per_intent_template_fields_default_to_none``
    guards): a record that carries no ``reason`` still returns None.

    First sentence, trimmed to a headline length. None when absent / non-string.

    NOTE (Recipe-SME / Compliance review): this deliberately fills the
    grandfather-clause null with the recipe's own reason rather than leaving the
    cell blank, per the 2026-06-15 product request. Revert this helper (and its
    two call sites) to restore the strict "blank on grandfathered intents"
    behaviour the 2026-05-28 panel signed off.
    """
    if record is None:
        return None
    rd = getattr(record, "resolution_data", None)
    if not isinstance(rd, dict):
        return None
    reason = rd.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    first = re.split(r"(?<=[.!?])\s", reason.strip())[0].strip()
    if len(first) <= _HEADLINE_MAX:
        return first
    return first[: _HEADLINE_MAX - 1].rstrip() + "…"
