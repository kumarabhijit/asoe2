"""Adapter registry: recipe outputs → AnalysisResponse enrichment fields.

The /analysis endpoint (api/routes/exceptions.py::get_analysis) consults
this registry to project the raw dict stored in
`record.resolution_data` into a typed Pydantic model on AnalysisResponse.
The projection lives here, not inside each recipe, because:

  * Recipes stay pure — they return plain dicts, no UI wire-format
    awareness. CLAUDE.md §1 (recipes execute, they don't present).
  * The adapter is the ONE place that knows both the event shape (for
    inputs like po_price) and the recipe output shape (for things like
    variance_pct). Keeping it adjacent to the API layer localises that
    knowledge.
  * Adding a new enrichment section = one pure function + one registry
    entry + one Pydantic model on schemas.py. No endpoint handler
    change, no recipe change.

Shadow-gated records: for YELLOW / RED shadow verdicts the orchestrator
short-circuits before `execute_recipe`, so `record.resolution_data` is
empty. Reviewers still need the enrichment (variance, sub_type,
classification etc.) to make their decision on those records. The
adapter calls the recipe SYNTHETICALLY for that case — recipes are
pure functions, so running one to produce a projection has no side
effects. Params are re-resolved from `record.original_event` + policy
constants, mirroring what `validate_types` in the orchestrator would
have done. Any exception (divide-by-zero, bad input) returns None;
the data-presence pattern keeps the UI honest.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from api.schemas import (
    AlternateDeliveryOption,
    DeliveryDelayAnalysisData,
    EdiMismatchAnalysisData,
    MOQAnalysisData,
    OverMaxAnalysisData,
    OverMaxLine,
    PriceHoldAnalysisData,
    RoundUpPlanLine,
    TrimPlanLine,
)
from api.store import ExceptionRecord
from contracts.policy import (
    DELIVERY_DELAY_MINOR_DAYS,
    DELIVERY_DELAY_SEVERE_DAYS,
    EDI_MISMATCH_AUTONOMY_LEVELS,
    MOQ_SEVERE_SHORTFALL_PCT,
    MOQ_UPLIFT_REVIEW_PCT,
    OVER_MAX_SEVERE_EXCEEDANCE_PCT,
    PRICE_HOLD_HARD_BLOCK_PCT,
    PRICE_HOLD_TOLERANCE_PCT,
)
from recipes.DeliveryDelayResolutionRecipe import resolve_delivery_delay
from recipes.EdiMismatchRecipe import detect_edi_mismatch
from recipes.MOQRoundUpRecipe import round_up_moq
from recipes.OverMaxTrimRecipe import trim_over_max
from recipes.PriceHoldReleaseRecipe import execute_price_hold_release


def _as_float(v: Any, default: float = 0.0) -> float:
    """Tolerant float coercion — returns `default` on None / non-numeric."""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _phr_from_outputs(
    outputs: Dict[str, Any], event: Dict[str, Any],
) -> Optional[PriceHoldAnalysisData]:
    """Shape a recipe-output dict (from record.resolution_data) into the
    UI model. `event` supplies po_price / sap_base_price / tolerance
    override that the recipe output doesn't echo back.
    """
    if outputs.get("action") is None or outputs.get("variance_pct") is None:
        return None
    metadata = event.get("metadata") or {}
    tolerance_pct = _as_float(
        metadata.get("tolerance_pct"), default=PRICE_HOLD_TOLERANCE_PCT
    )
    try:
        return PriceHoldAnalysisData(
            hold_status="RELEASED" if outputs.get("status") == "RELEASED" else "HELD",
            po_price=_as_float(event.get("po_price")),
            sap_base_price=_as_float(event.get("sap_base_price")),
            variance_pct=_as_float(outputs.get("variance_pct")),
            tolerance_pct=tolerance_pct,
            hard_block_pct=PRICE_HOLD_HARD_BLOCK_PCT,
            action=outputs.get("action"),
            reason=str(outputs.get("reason", "")),
        )
    except (TypeError, ValueError):
        return None


def adapt_price_hold(record: ExceptionRecord) -> Optional[PriceHoldAnalysisData]:
    """Project a PriceHoldReleaseRecipe resolution into PriceHoldAnalysisData.

    Three source paths, in order of preference:
      1. `record.resolution_data` has recipe output (GREEN shadow path).
      2. Recipe didn't run (YELLOW/RED shadow gated the record before
         `execute_recipe`). Synthesize the projection by calling the
         recipe directly — it's pure. Reviewers still see the variance
         analysis even though the orchestrator blocked auto-execution.
      3. Event data insufficient (e.g. sap_base_price <= 0) — return
         None, section doesn't mount.
    """
    outputs = record.resolution_data or {}
    event = record.original_event or {}
    status = outputs.get("status")

    if status and status != "FAILED":
        projection = _phr_from_outputs(outputs, event)
        if projection is not None:
            return projection

    # Synthetic path — call the pure recipe function with params re-
    # resolved from the event. Mirror the resolution `validate_types`
    # does at orchestration/nodes.py::validate_types PriceHold branch.
    sap_base = _as_float(event.get("sap_base_price"))
    if sap_base <= 0:
        return None
    metadata = event.get("metadata") or {}
    tolerance_pct = _as_float(
        metadata.get("tolerance_pct"), default=PRICE_HOLD_TOLERANCE_PCT
    )
    try:
        synthetic = execute_price_hold_release(
            order_id=str(event.get("order_id", "")),
            line_item=int(event.get("line_item") or 1),
            po_price=_as_float(event.get("po_price")),
            sap_base_price=sap_base,
            tolerance_pct=tolerance_pct,
            hard_block_pct=PRICE_HOLD_HARD_BLOCK_PCT,
            hold_status=metadata.get("price_hold_status", "HELD"),
        )
    except (AssertionError, ZeroDivisionError, TypeError, ValueError):
        return None
    if synthetic.get("status") == "FAILED":
        return None
    return _phr_from_outputs(synthetic, event)


def _edi_from_outputs(
    outputs: Dict[str, Any],
) -> Optional[EdiMismatchAnalysisData]:
    classification = outputs.get("classification")
    autonomy_level = outputs.get("autonomy_level")
    if classification is None or autonomy_level is None:
        return None
    try:
        return EdiMismatchAnalysisData(
            sub_type=str(outputs.get("sub_type", "")),
            classification=classification,
            recommended_action=str(outputs.get("recommended_action", "")),
            autonomy_level=autonomy_level,
            expected_value=outputs.get("expected_value"),
            received_value=outputs.get("received_value"),
            notification_template=outputs.get("notification_template"),
        )
    except (TypeError, ValueError):
        return None


def adapt_edi_mismatch(record: ExceptionRecord) -> Optional[EdiMismatchAnalysisData]:
    """Project an EdiMismatchRecipe resolution into EdiMismatchAnalysisData.

    Same three-path structure as `adapt_price_hold`:
      1. Recipe output present (GREEN: QTY_MISMATCH / UOM_MISMATCH).
      2. Shadow gated (RED: SKU_MISMATCH; YELLOW: SHIP_TO_MISMATCH) —
         call the recipe synthetically with event metadata.
      3. Routing error (PRICE_MISMATCH reached the recipe, or unknown
         sub_type) → recipe returns FAILED → adapter returns None.

    PRICE_MISMATCH: the classifier routes it to CONTRACTUAL_CORRECTION
    so this adapter is never even consulted for those records. The
    explicit FAILED gate below is defence-in-depth if the invariant
    ever breaks.
    """
    outputs = record.resolution_data or {}
    event = record.original_event or {}
    status = outputs.get("status")

    if status and status != "FAILED":
        projection = _edi_from_outputs(outputs)
        if projection is not None:
            return projection

    metadata = event.get("metadata") or {}
    sub_type = metadata.get("mismatch_sub_type")
    if sub_type is None:
        return None
    try:
        synthetic = detect_edi_mismatch(
            order_id=str(event.get("order_id", "")),
            sub_type=str(sub_type),
            expected_value=metadata.get("expected_value"),
            received_value=metadata.get("received_value"),
            autonomy_levels=EDI_MISMATCH_AUTONOMY_LEVELS,
        )
    except (TypeError, ValueError):
        return None
    if synthetic.get("status") == "FAILED":
        return None
    return _edi_from_outputs(synthetic)


def _ranked_option_to_model(
    raw: Dict[str, Any], *, recommended: bool = False,
) -> Optional[AlternateDeliveryOption]:
    """Coerce one recipe-emitted alternate option into the typed
    Pydantic model. Returns None on shape drift so the caller can
    silently drop rather than poison the list."""
    if not isinstance(raw, dict):
        return None
    raw_type = raw.get("type")
    if not raw_type:
        return None
    try:
        return AlternateDeliveryOption(
            id=str(raw.get("id") or raw_type),
            type=str(raw_type),
            title=str(raw.get("title") or raw_type),
            description=str(raw.get("description") or ""),
            new_eta=raw.get("new_eta"),
            extra_cost=_as_float(raw.get("extra_cost")),
            recommended=bool(raw.get("recommended", recommended)),
        )
    except (TypeError, ValueError):
        return None


def _delivery_delay_from_outputs(
    outputs: Dict[str, Any], event: Dict[str, Any],
) -> Optional[DeliveryDelayAnalysisData]:
    """Shape a DeliveryDelayResolutionRecipe output dict into the UI
    model. Event supplies planned_date / projected_eta / line_count
    because the recipe doesn't echo them back."""
    metadata = event.get("metadata") or {}
    planned = metadata.get("planned_date")
    projected = metadata.get("projected_eta")
    if not planned or not projected:
        return None

    days_late = outputs.get("days_late")
    if days_late is None:
        return None

    primary_raw = outputs.get("primary_option")
    primary_id: Optional[str] = None
    if isinstance(primary_raw, dict):
        primary_id = primary_raw.get("id") or primary_raw.get("type")

    ranked_raw = outputs.get("alternate_options") or []
    alternates: List[AlternateDeliveryOption] = []
    for opt in ranked_raw:
        is_primary = (
            isinstance(opt, dict)
            and (opt.get("id") == primary_id or opt.get("type") == primary_id)
        )
        model = _ranked_option_to_model(opt, recommended=bool(is_primary))
        if model is not None:
            alternates.append(model)

    try:
        return DeliveryDelayAnalysisData(
            planned_date=str(planned),
            projected_eta=str(projected),
            days_late=int(days_late),
            delay_category=str(
                outputs.get("delay_category")
                or metadata.get("delay_category")
                or "UNSPECIFIED"
            ),
            affected_lines=int(event.get("line_count") or 1),
            # at_risk + sla_deadline — gateway-dependent; grandfathered
            # under delivery_delay_financial_gap until 2026-07-21.
            at_risk=None,
            sla_deadline=metadata.get("sla_deadline"),
            alternate_options=alternates,
            delay_reason=metadata.get("delay_reason"),
            carrier=outputs.get("carrier") or metadata.get("carrier"),
            route=outputs.get("route") or metadata.get("route"),
            rule_id=metadata.get("rule_id"),
        )
    except (TypeError, ValueError):
        return None


def adapt_delivery_delay(
    record: ExceptionRecord,
) -> Optional[DeliveryDelayAnalysisData]:
    """Project a DeliveryDelayResolutionRecipe resolution into
    DeliveryDelayAnalysisData.

    Three-path lookup:
      1. Recipe output in `record.resolution_data` (GREEN path).
      2. Shadow-gated (YELLOW SEVERE / YELLOW MINOR) — synthesise
         by invoking the pure recipe with event-sourced params.
      3. Event lacks planned/projected dates → None (defensive).
    """
    outputs = record.resolution_data or {}
    event = record.original_event or {}
    status = outputs.get("status")

    if status and status not in ("FAILED",):
        projection = _delivery_delay_from_outputs(outputs, event)
        if projection is not None:
            return projection

    metadata = event.get("metadata") or {}
    planned = metadata.get("planned_date")
    projected = metadata.get("projected_eta")
    if not planned or not projected:
        return None
    try:
        synthetic = resolve_delivery_delay(
            order_id=str(event.get("order_id", "")),
            planned_date=str(planned),
            projected_eta=str(projected),
            minor_days=DELIVERY_DELAY_MINOR_DAYS,
            severe_days=DELIVERY_DELAY_SEVERE_DAYS,
            carrier=metadata.get("carrier"),
            route=metadata.get("route"),
            delay_category=metadata.get("delay_category"),
            alternate_options=metadata.get("alternate_options") or [],
        )
    except (TypeError, ValueError):
        return None
    if synthetic.get("status") == "FAILED":
        return None
    return _delivery_delay_from_outputs(synthetic, event)


def _coerce_overmax_line(raw: Any) -> Optional[OverMaxLine]:
    if not isinstance(raw, dict) or "sku" not in raw:
        return None
    try:
        return OverMaxLine(
            sku=str(raw["sku"]),
            description=str(raw.get("description") or ""),
            qty=_as_float(raw.get("qty")),
            max_line_qty=(
                _as_float(raw.get("max_line_qty"))
                if raw.get("max_line_qty") is not None else None
            ),
            excess=_as_float(raw.get("excess")),
            is_even_layer_item=bool(raw.get("is_even_layer_item", False)),
        )
    except (TypeError, ValueError):
        return None


def _coerce_trim_plan_line(raw: Any) -> Optional[TrimPlanLine]:
    if not isinstance(raw, dict) or "sku" not in raw:
        return None
    action = raw.get("action")
    if action not in ("TRIM", "SKIP", "OK"):
        action = "TRIM"
    try:
        return TrimPlanLine(
            sku=str(raw["sku"]),
            description=str(raw.get("description") or ""),
            ordered=_as_float(raw.get("ordered")),
            trimmed_to=_as_float(raw.get("trimmed_to")),
            delta=_as_float(raw.get("delta")),
            action=action,
        )
    except (TypeError, ValueError):
        return None


def _overmax_from_outputs(
    outputs: Dict[str, Any], event: Dict[str, Any],
) -> Optional[OverMaxAnalysisData]:
    metadata = event.get("metadata") or {}
    total_ordered = _as_float(metadata.get("total_ordered"))
    max_qty = _as_float(metadata.get("max_qty"))
    if total_ordered <= 0 or max_qty <= 0:
        return None
    excess_qty = outputs.get("excess_qty")
    exceedance_pct = outputs.get("exceedance_pct")
    if excess_qty is None or exceedance_pct is None:
        return None

    raw_lines = metadata.get("order_lines") or []
    order_lines: List[OverMaxLine] = []
    for r in raw_lines:
        m = _coerce_overmax_line(r)
        if m is not None:
            order_lines.append(m)

    raw_plan = outputs.get("trim_plan") or []
    trim_plan: List[TrimPlanLine] = []
    for r in raw_plan:
        m = _coerce_trim_plan_line(r)
        if m is not None:
            trim_plan.append(m)

    try:
        return OverMaxAnalysisData(
            total_ordered=total_ordered,
            max_qty=max_qty,
            excess_qty=_as_float(excess_qty),
            exceedance_pct=_as_float(exceedance_pct),
            uom=str(metadata.get("uom") or ""),
            at_risk=_as_float(outputs.get("at_risk")),
            order_lines=order_lines,
            trim_plan=trim_plan,
            # Grandfathered fields — populated when SAP gateway
            # lands. Optional in the model so coverage doesn't fail
            # while the overmax_gateway_gap clause is active.
            contract_ref=metadata.get("contract_ref"),
            block_status=metadata.get("block_status"),
            block_reason=metadata.get("block_reason"),
        )
    except (TypeError, ValueError):
        return None


def adapt_overmax(record: ExceptionRecord) -> Optional[OverMaxAnalysisData]:
    """Project an OverMaxTrimRecipe resolution into OverMaxAnalysisData.

    Three-path lookup mirroring the other adapters: recipe output,
    synthetic invocation when shadow gated, defensive None on missing
    event metadata.
    """
    outputs = record.resolution_data or {}
    event = record.original_event or {}
    status = outputs.get("status")

    if status and status != "FAILED":
        projection = _overmax_from_outputs(outputs, event)
        if projection is not None:
            return projection

    metadata = event.get("metadata") or {}
    total_ordered = _as_float(metadata.get("total_ordered"))
    max_qty = _as_float(metadata.get("max_qty"))
    if total_ordered <= 0 or max_qty <= 0:
        return None
    try:
        synthetic = trim_over_max(
            order_id=str(event.get("order_id", "")),
            total_ordered=total_ordered,
            max_qty=max_qty,
            severe_exceedance_pct=OVER_MAX_SEVERE_EXCEEDANCE_PCT,
            order_lines=metadata.get("order_lines"),
            unit_cost_per_line=metadata.get("unit_cost_per_line"),
        )
    except (TypeError, ValueError):
        return None
    if synthetic.get("status") == "FAILED":
        return None
    return _overmax_from_outputs(synthetic, event)


def _moq_from_outputs(
    outputs: Dict[str, Any], event: Dict[str, Any],
) -> Optional[MOQAnalysisData]:
    metadata = event.get("metadata") or {}
    ordered_qty = _as_float(metadata.get("ordered_qty"))
    moq_qty = _as_float(metadata.get("moq_qty"))
    if ordered_qty < 0 or moq_qty <= 0:
        return None
    shortfall_qty = outputs.get("shortfall_qty")
    shortfall_pct = outputs.get("shortfall_pct")
    if shortfall_qty is None or shortfall_pct is None:
        return None

    sku = (
        event.get("sku")
        or metadata.get("sku")
        or outputs.get("sku")
        or ""
    )

    raw_plan = outputs.get("round_up_plan")
    plan: List[RoundUpPlanLine] = []
    if isinstance(raw_plan, dict):
        # Recipe currently emits a single dict, not a list. Wrap it.
        action = raw_plan.get("action")
        if action not in ("ROUND_UP", "ACCEPT_BELOW", "ESCALATE"):
            action = "ROUND_UP"
        try:
            plan.append(RoundUpPlanLine(
                sku=str(raw_plan.get("sku") or sku),
                description=str(raw_plan.get("description") or ""),
                ordered=_as_float(raw_plan.get("ordered")),
                round_up_to=_as_float(raw_plan.get("round_up_to")),
                delta=_as_float(raw_plan.get("delta")),
                action=action,
            ))
        except (TypeError, ValueError):
            pass

    try:
        return MOQAnalysisData(
            ordered_qty=ordered_qty,
            moq_qty=moq_qty,
            shortfall_qty=_as_float(shortfall_qty),
            shortfall_pct=_as_float(shortfall_pct),
            sku=str(sku),
            unit_cost=_as_float(metadata.get("unit_cost")),
            uom=str(metadata.get("uom") or ""),
            at_risk=_as_float(outputs.get("uplift_value")),
            round_up_plan=plan,
            # Grandfathered audit-bearing — present iff metadata
            # supplies them today; gateway will populate post-deadline.
            moq_source=metadata.get("moq_source"),
            channel=metadata.get("channel"),
            contract_ref=metadata.get("contract_ref"),
            block_status=metadata.get("block_status"),
            description=metadata.get("description"),
            block_message=metadata.get("block_message"),
        )
    except (TypeError, ValueError):
        return None


def adapt_moq(record: ExceptionRecord) -> Optional[MOQAnalysisData]:
    """Project an MOQRoundUpRecipe resolution into MOQAnalysisData.

    Three-path lookup. at_risk surfaces the recipe's `uplift_value`
    (uplift_qty × unit_cost) — already the right number for the
    four-eyes financial-impact gate.
    """
    outputs = record.resolution_data or {}
    event = record.original_event or {}
    status = outputs.get("status")

    if status and status != "FAILED":
        projection = _moq_from_outputs(outputs, event)
        if projection is not None:
            return projection

    metadata = event.get("metadata") or {}
    ordered_qty = _as_float(metadata.get("ordered_qty"))
    moq_qty = _as_float(metadata.get("moq_qty"))
    if ordered_qty < 0 or moq_qty <= 0:
        return None
    try:
        synthetic = round_up_moq(
            order_id=str(event.get("order_id", "")),
            sku=str(
                event.get("sku") or metadata.get("sku") or ""
            ),
            ordered_qty=ordered_qty,
            moq_qty=moq_qty,
            unit_cost=_as_float(metadata.get("unit_cost")),
            uom=str(metadata.get("uom") or "CS"),
            severe_shortfall_pct=MOQ_SEVERE_SHORTFALL_PCT,
            uplift_review_pct=MOQ_UPLIFT_REVIEW_PCT,
        )
    except (TypeError, ValueError):
        return None
    if synthetic.get("status") == "FAILED":
        return None
    return _moq_from_outputs(synthetic, event)


# Recipe-name → (target field on AnalysisResponse, adapter function).
#
# The endpoint looks up by `record.selected_recipe`. Absent recipe name
# = no enrichment. New enrichment sections: add the three artifacts
# (Pydantic model, adapter function, registry row) and the UI section
# picks it up via the data-presence pattern.
ANALYSIS_ADAPTERS: Dict[
    str, Tuple[str, Callable[[ExceptionRecord], Any]]
] = {
    "PriceHoldReleaseRecipe.py": ("price_hold_analysis", adapt_price_hold),
    "EdiMismatchRecipe.py": ("edi_mismatch_analysis", adapt_edi_mismatch),
    "DeliveryDelayResolutionRecipe.py": (
        "delivery_delay_analysis", adapt_delivery_delay,
    ),
    "OverMaxTrimRecipe.py": ("overmax_analysis", adapt_overmax),
    "MOQRoundUpRecipe.py": ("moq_analysis", adapt_moq),
}


# Intent → recipe-name fallback. Used by the endpoint when
# `record.selected_recipe` is None (shadow_audit gated before
# select_recipe ran). `record.intent` is always set by
# classify_intent which runs earlier, so this gives the adapter
# enough signal to produce a synthetic projection.
INTENT_TO_RECIPE_NAME: Dict[str, str] = {
    "PRICE_HOLD_RELEASE": "PriceHoldReleaseRecipe.py",
    "EDI_MISMATCH": "EdiMismatchRecipe.py",
    "DELIVERY_DELAY": "DeliveryDelayResolutionRecipe.py",
    "OVER_MAX": "OverMaxTrimRecipe.py",
    "MIN_ORDER_QTY": "MOQRoundUpRecipe.py",
}


def resolve_adapter_key(record: ExceptionRecord) -> Optional[str]:
    """Pick the registry key for this record: selected_recipe first,
    intent→recipe fallback second. Keeps the shadow-gated case
    (selected_recipe=None) in scope for projection."""
    if record.selected_recipe:
        return record.selected_recipe
    if record.intent:
        return INTENT_TO_RECIPE_NAME.get(record.intent)
    return None
