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
    AlternateWarehouse,
    BackOrderAnalysisData,
    ComparisonLineItem,
    ComparisonOrder,
    DeliveryDelayAnalysisData,
    DuplicateDetectionData,
    EdiMismatchAnalysisData,
    EmailAttachmentManifestEntry,
    EmailOrderEntryAnalysisData,
    EmailOrderEntryFloorStatus,
    EmailSourceData,
    InboundOrder,
    MOQAnalysisData,
    OrderComparisonData,
    OrderSnapshot,
    OverMaxAnalysisData,
    OverMaxLine,
    PalletAnalysisData,
    PriceAnalysisData,
    PalletLine,
    PalletSuggestion,
    PriceHoldAnalysisData,
    ResolutionOption,
    ResolutionOptionScores,
    RoundUpPlanLine,
    SubstituteSKU,
    TrimPlanLine,
    WarehouseInfo,
)
from api.store import ExceptionRecord
from contracts.policy import (
    BACK_ORDER_SEVERE_GAP_PCT,
    DELIVERY_DELAY_MINOR_DAYS,
    DELIVERY_DELAY_SEVERE_DAYS,
    DUPLICATE_PO_AUTONOMY_LEVELS,
    DUPLICATE_PO_THRESHOLD_AUTO_BLOCK,
    DUPLICATE_PO_THRESHOLD_REVIEW_REQUIRED,
    DUPLICATE_PO_THRESHOLD_SOFT_FLAG,
    EDI_MISMATCH_AUTONOMY_LEVELS,
    EMAIL_ORDER_AUTO_APPROVE_CONFIDENCE,
    EMAIL_ORDER_AUTO_CORRECT_CONFIDENCE,
    EMAIL_ORDER_ENTRY_AUTONOMY_LEVELS,
    EMAIL_ORDER_REVIEW_BAND_LOW,
    MOQ_SEVERE_SHORTFALL_PCT,
    MOQ_UPLIFT_REVIEW_PCT,
    OVER_MAX_SEVERE_EXCEEDANCE_PCT,
    PALLET_CONFIG_BROKEN_LAYER_FILL_PCT,
    PALLET_CONFIG_MIN_FILL_PCT,
    PRICE_HOLD_HARD_BLOCK_PCT,
    PRICE_HOLD_TOLERANCE_PCT,
)
from recipes.BackOrderResolutionRecipe import resolve_back_order
from recipes.DeliveryDelayResolutionRecipe import resolve_delivery_delay
from recipes.DuplicatePORecipe import detect_duplicate_po
from recipes.EdiMismatchRecipe import detect_edi_mismatch
from recipes.EmailOrderEntryRecipe import (
    FLOOR_KEYS as _EMAIL_ORDER_FLOOR_KEYS,
    classify_email_order_entry,
)
from recipes.MOQRoundUpRecipe import round_up_moq
from recipes.OverMaxTrimRecipe import trim_over_max
from recipes.PalletAlignmentRecipe import align_pallets
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
    sla_ctx: Optional[Dict[str, Any]] = None,
) -> Optional[DeliveryDelayAnalysisData]:
    """Shape a DeliveryDelayResolutionRecipe output dict into the UI
    model. Event supplies planned_date / projected_eta / line_count
    because the recipe doesn't echo them back. `sla_ctx` (T5) carries
    at_risk + sla_deadline from the SLA contract gateway."""
    sla_ctx = sla_ctx or {}
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
            # T5: at_risk + sla_deadline now sourced from sla_contract
            # gateway via enrichment_context (delivery_delay_financial_gap
            # retired). Falls back to event metadata if the gateway
            # didn't run (shadow-gated paths).
            at_risk=_as_float(
                sla_ctx.get("at_risk"), default=None
            ) if sla_ctx.get("at_risk") is not None else metadata.get("at_risk"),
            sla_deadline=sla_ctx.get("sla_deadline") or metadata.get("sla_deadline"),
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
    sla_ctx = (record.enrichment_context or {}).get("sla_contract_context") or {}
    status = outputs.get("status")

    if status and status not in ("FAILED",):
        projection = _delivery_delay_from_outputs(outputs, event, sla_ctx)
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
    return _delivery_delay_from_outputs(synthetic, event, sla_ctx)


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
    contract_ctx: Optional[Dict[str, Any]] = None,
    block_ctx: Optional[Dict[str, Any]] = None,
) -> Optional[OverMaxAnalysisData]:
    metadata = event.get("metadata") or {}
    contract_ctx = contract_ctx or {}
    block_ctx = block_ctx or {}
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
            # T5: contract_ref / block_status / block_reason now from
            # gateway via enrichment_context (overmax_gateway_gap
            # retired). Falls back to event metadata if the gateway
            # didn't run (shadow-gated paths).
            contract_ref=contract_ctx.get("contract_ref")
            or metadata.get("contract_ref"),
            block_status=block_ctx.get("block_status")
            or metadata.get("block_status"),
            block_reason=block_ctx.get("block_reason")
            or metadata.get("block_reason"),
        )
    except (TypeError, ValueError):
        return None


def adapt_overmax(record: ExceptionRecord) -> Optional[OverMaxAnalysisData]:
    """Project an OverMaxTrimRecipe resolution into OverMaxAnalysisData.

    Three-path lookup mirroring the other adapters: recipe output,
    synthetic invocation when shadow gated, defensive None on missing
    event metadata. T5: contract_ref / block_status / block_reason
    sourced from sap_contract + sap_block gateway via
    enrichment_context (overmax_gateway_gap retired).
    """
    outputs = record.resolution_data or {}
    event = record.original_event or {}
    enrichment = record.enrichment_context or {}
    contract_ctx = enrichment.get("contract_context") or {}
    block_ctx = enrichment.get("block_context") or {}
    status = outputs.get("status")

    if status and status != "FAILED":
        projection = _overmax_from_outputs(outputs, event, contract_ctx, block_ctx)
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
    return _overmax_from_outputs(synthetic, event, contract_ctx, block_ctx)


def _moq_from_outputs(
    outputs: Dict[str, Any], event: Dict[str, Any],
    cm_ctx: Optional[Dict[str, Any]] = None,
    contract_ctx: Optional[Dict[str, Any]] = None,
    block_ctx: Optional[Dict[str, Any]] = None,
) -> Optional[MOQAnalysisData]:
    cm_ctx = cm_ctx or {}
    contract_ctx = contract_ctx or {}
    block_ctx = block_ctx or {}
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
            # T5: moq_source / channel / contract_ref / block_status
            # now from gateway via enrichment_context (moq_gateway_gap
            # retired). Falls back to event metadata if the gateway
            # didn't run (shadow-gated paths).
            moq_source=cm_ctx.get("moq_source") or metadata.get("moq_source"),
            channel=cm_ctx.get("channel") or metadata.get("channel"),
            contract_ref=contract_ctx.get("contract_ref")
            or metadata.get("contract_ref"),
            block_status=block_ctx.get("block_status")
            or metadata.get("block_status"),
            description=metadata.get("description"),
            block_message=block_ctx.get("block_message")
            or metadata.get("block_message"),
        )
    except (TypeError, ValueError):
        return None


def adapt_moq(record: ExceptionRecord) -> Optional[MOQAnalysisData]:
    """Project an MOQRoundUpRecipe resolution into MOQAnalysisData.

    Three-path lookup. at_risk surfaces the recipe's `uplift_value`
    (uplift_qty × unit_cost) — already the right number for the
    four-eyes financial-impact gate. T5: moq_source / channel /
    contract_ref / block_status sourced from sap_customer_master /
    sap_contract / sap_block via enrichment_context (moq_gateway_gap
    retired).
    """
    outputs = record.resolution_data or {}
    event = record.original_event or {}
    enrichment = record.enrichment_context or {}
    cm_ctx = enrichment.get("customer_master_context") or {}
    contract_ctx = enrichment.get("contract_context") or {}
    block_ctx = enrichment.get("block_context") or {}
    status = outputs.get("status")

    if status and status != "FAILED":
        projection = _moq_from_outputs(
            outputs, event, cm_ctx, contract_ctx, block_ctx,
        )
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
    return _moq_from_outputs(synthetic, event, cm_ctx, contract_ctx, block_ctx)


def _coerce_pallet_line(raw: Any) -> Optional[PalletLine]:
    if not isinstance(raw, dict) or "sku" not in raw:
        return None
    try:
        return PalletLine(
            sku=str(raw["sku"]),
            description=str(raw.get("description") or ""),
            uom=str(raw.get("uom") or ""),
            layer_qty=_as_float(raw.get("layer_qty")),
            pallet_qty=_as_float(raw.get("pallet_qty")),
            ordered_qty=_as_float(raw.get("ordered_qty")),
            complete_layers=int(raw.get("complete_layers") or 0),
            loose_qty=_as_float(raw.get("loose_qty")),
            full_pallets=int(raw.get("full_pallets") or 0),
            pallet_fill_pct=_as_float(raw.get("pallet_fill_pct")),
            violation_type=raw.get("violation_type"),
        )
    except (TypeError, ValueError):
        return None


def _coerce_pallet_suggestion(raw: Any) -> Optional[PalletSuggestion]:
    if not isinstance(raw, dict) or "sku" not in raw:
        return None
    try:
        return PalletSuggestion(
            sku=str(raw["sku"]),
            description=str(raw.get("description") or ""),
            current=_as_float(raw.get("current")),
            suggested=_as_float(raw.get("suggested")),
            delta=_as_float(raw.get("delta")),
            layers=int(raw.get("layers") or 0),
            full_pallets=int(raw.get("full_pallets") or 0),
            reason=str(raw.get("reason") or ""),
        )
    except (TypeError, ValueError):
        return None


def _pallet_from_outputs(
    outputs: Dict[str, Any],
) -> Optional[PalletAnalysisData]:
    classification = outputs.get("classification")
    if classification is None:
        return None
    raw_lines = outputs.get("lines") or []
    raw_plan = outputs.get("suggested_plan") or []
    lines = [m for m in (_coerce_pallet_line(r) for r in raw_lines) if m]
    plan = [m for m in (_coerce_pallet_suggestion(r) for r in raw_plan) if m]
    if not lines:
        # Recipe couldn't produce per-line data — projection has no
        # meaningful Layer-2 surface. Composer will mark
        # AUDIT_CONTEXT_MISSING.
        return None
    try:
        return PalletAnalysisData(
            total_ordered_cases=_as_float(outputs.get("total_ordered_cases")),
            loose_cases_total=_as_float(outputs.get("loose_cases_total")),
            order_line_count=int(outputs.get("order_line_count") or len(lines)),
            classification=str(classification),
            lines=lines,
            suggested_plan=plan,
        )
    except (TypeError, ValueError):
        return None


def adapt_pallet(record: ExceptionRecord) -> Optional[PalletAnalysisData]:
    """Project a PalletAlignmentRecipe resolution into PalletAnalysisData.

    Recipe is shadow-gated YELLOW for any non-conforming pallet, so
    the synthetic fallback path is the most common.
    """
    outputs = record.resolution_data or {}
    event = record.original_event or {}
    status = outputs.get("status")

    if status and status != "FAILED":
        projection = _pallet_from_outputs(outputs)
        if projection is not None:
            return projection

    metadata = event.get("metadata") or {}
    # Source-of-truth key is `pallet_lines` (orchestration/nodes.py
    # validate_types reads metadata.pallet_lines for this recipe).
    # Tolerate `lines` / `order_lines` as fallbacks since e2e
    # fixtures and synthesis tests use both shapes.
    raw_lines = (
        metadata.get("pallet_lines")
        or metadata.get("lines")
        or metadata.get("order_lines")
        or []
    )
    if not raw_lines:
        return None
    try:
        synthetic = align_pallets(
            order_id=str(event.get("order_id", "")),
            lines=raw_lines,
            min_fill_pct=PALLET_CONFIG_MIN_FILL_PCT,
            broken_layer_fill_pct=PALLET_CONFIG_BROKEN_LAYER_FILL_PCT,
        )
    except (TypeError, ValueError):
        return None
    if synthetic.get("status") == "FAILED":
        return None
    return _pallet_from_outputs(synthetic)


# ─── Duplicate-PO adapters (T2) ─────────────────────────────────────────
#
# DuplicatePO is unique among the recipes shipped so far: gateway
# evidence (the matched OrderSnapshot pair from
# oms/get_matched_po_details) is the audit-bearing source of truth.
# The recipe itself produces only classification + recommended_action
# — the operator-facing OrderSnapshot pair, days_between, and
# cancellation_target live in `record.enrichment_context`
# (Verdict Pillar 1). The composer enforces audit-bearing coverage
# against DuplicateDetectionData; OrderComparisonData is synthesised
# from the same payload (registry: "Synthesised from DuplicateDetection;
# same attestation target") and carried as a SECONDARY adapter.

def _format_autonomy(level: Any, action: Any) -> str:
    """Format the autonomy_applied display string ("L1 — BLOCK_AND_NOTIFY").

    Mirrors the convention used in the EDI mismatch synthesis path —
    operator-readable, attribution-friendly. Returns "" when the
    recipe didn't emit a level.
    """
    if not level:
        return ""
    if action:
        return f"{level} — {action}"
    return str(level)


def _order_snapshot(payload: Optional[Dict[str, Any]]) -> Optional[OrderSnapshot]:
    """Coerce a gateway snapshot dict into the typed model. Returns
    None when any audit-bearing subfield is missing — the composer
    then routes to AUDIT_CONTEXT_MISSING."""
    if not payload:
        return None
    required = ("so_number", "po_number", "created_date",
                "total_value", "line_count", "status")
    if any(payload.get(f) in (None, "") for f in required):
        return None
    try:
        return OrderSnapshot(
            so_number=str(payload["so_number"]),
            po_number=str(payload["po_number"]),
            created_date=str(payload["created_date"]),
            total_value=_as_float(payload["total_value"]),
            line_count=int(payload["line_count"]),
            status=str(payload["status"]),
        )
    except (TypeError, ValueError):
        return None


def _synthesize_duplicate_outputs(
    record: ExceptionRecord, matched: Dict[str, Any],
) -> Dict[str, Any]:
    """Run DuplicatePORecipe synthetically to fill recipe-output fields
    on the explain / shadow-gated path. The recipe is pure (no side
    effects), so calling it from the adapter is safe and matches the
    pattern used by adapt_price_hold and adapt_pallet."""
    event = record.original_event or {}
    metadata = event.get("metadata") or {}
    signal_scores = metadata.get("signal_scores") or {}
    if not isinstance(signal_scores, dict):
        return {}
    try:
        return detect_duplicate_po(
            incoming_po_number=str(event.get("order_id", "")),
            customer_id=str(event.get("retailer_id", "") or ""),
            signal_scores={k: _as_float(v) for k, v in signal_scores.items()},
            threshold_auto_block=DUPLICATE_PO_THRESHOLD_AUTO_BLOCK,
            threshold_review_required=DUPLICATE_PO_THRESHOLD_REVIEW_REQUIRED,
            threshold_soft_flag=DUPLICATE_PO_THRESHOLD_SOFT_FLAG,
            original_fulfilled=(matched.get("original_fulfilled")
                                if isinstance(matched.get("original_fulfilled"), bool)
                                else None),
            has_revision_indicator=matched.get("has_revision_indicator"),
            line_items_identical=matched.get("line_items_identical"),
            autonomy_levels=DUPLICATE_PO_AUTONOMY_LEVELS,
        )
    except (TypeError, ValueError):
        return {}


def adapt_duplicate(record: ExceptionRecord) -> Optional[DuplicateDetectionData]:
    """Project DuplicatePORecipe into DuplicateDetectionData.

    Two source bags:
      * `record.enrichment_context["matched_po_details"]` — gateway
        snapshot pair + days_between + cancellation_target +
        detection_method. Audit-bearing per
        compliance/audit_bearing_registry.yaml::DuplicateDetectionData.
      * `record.resolution_data` — recipe composite_score (→
        confidence × 100), recommended_action, autonomy_level.

    Source paths, in order of preference:
      1. resolution_data has recipe output → use it (live GREEN path).
      2. resolution_data empty → synthesize via pure recipe call so
         explain mode + shadow-gated records still get audit-complete
         projections (mirrors adapt_price_hold's pattern).

    Returns None when the gateway evidence (matched OrderSnapshot pair)
    is absent — Pillar 2 then routes to AUDIT_CONTEXT_MISSING via
    build_analysis. No grandfather clause in this engagement.
    """
    matched = (record.enrichment_context or {}).get("matched_po_details") or {}
    original = _order_snapshot(matched.get("original_order"))
    duplicate = _order_snapshot(matched.get("duplicate_order"))
    if original is None or duplicate is None:
        return None

    outputs = record.resolution_data or {}
    if not outputs.get("recommended_action"):
        outputs = _synthesize_duplicate_outputs(record, matched)

    try:
        return DuplicateDetectionData(
            original_order=original,
            duplicate_order=duplicate,
            detection_method=matched.get("detection_method"),
            days_between=int(matched.get("days_between") or 0),
            confidence=_as_float(outputs.get("composite_score")) * 100.0,
            recommended_action=str(outputs.get("recommended_action") or ""),
            cancellation_target=str(matched.get("cancellation_target") or ""),
            autonomy_applied=_format_autonomy(
                outputs.get("autonomy_level"),
                outputs.get("recommended_action"),
            ),
        )
    except (TypeError, ValueError):
        return None


def _comparison_order(
    payload: Optional[Dict[str, Any]], customer: str,
) -> Optional[ComparisonOrder]:
    if not payload:
        return None
    try:
        lines = [
            ComparisonLineItem(
                sku=str(li.get("sku", "")),
                description=str(li.get("description", "") or ""),
                qty=_as_float(li.get("qty")),
                unit_price=_as_float(li.get("unit_price")),
            )
            for li in (payload.get("lines") or [])
            if li.get("sku")
        ]
        return ComparisonOrder(
            so_number=str(payload.get("so_number", "")),
            po_number=str(payload.get("po_number", "")),
            created_date=str(payload.get("created_date", "")),
            customer=customer,
            lines=lines,
            total_value=_as_float(payload.get("total_value")),
            status=str(payload.get("status", "")),
        )
    except (TypeError, ValueError):
        return None


def adapt_order_comparison(
    record: ExceptionRecord,
) -> Optional[OrderComparisonData]:
    """Synthesise side-by-side comparison from `matched_po_details`.

    Single source of truth (R5) — re-projects the same gateway payload
    that drives DuplicateDetectionData. No separate recipe.

    Returns None when no matched-PO snapshot pair is present.
    """
    matched = (record.enrichment_context or {}).get("matched_po_details") or {}
    customer = str(
        matched.get("customer_id")
        or (record.original_event or {}).get("retailer_id")
        or ""
    )
    orig = _comparison_order(matched.get("original_order"), customer)
    dup = _comparison_order(matched.get("duplicate_order"), customer)
    if orig is None or dup is None:
        return None
    return OrderComparisonData(
        orders=[orig, dup],
        matching_fields=list(matched.get("matching_fields") or []),
        differing_fields=list(matched.get("differing_fields") or []),
    )


# ─── Price adapter (T4 — retires price_analysis_gateway_gap) ──────────
#
# PriceAdjustmentRecipe now carries three gateway dependencies
# (sap_doc / sap_contract / promotion — see recipes/registry.py). Their
# payloads land in enrichment_context under result keys
# `sap_doc_context`, `contract_context`, `promotion_context`. The
# adapter projects them into PriceAnalysisData; missing audit-bearing
# gateway fields route the exception to AUDIT_CONTEXT_MISSING (no
# grandfather clause — retired in this engagement).
#
# Expected gateway response shapes:
#
#   sap_doc_context    → {doc_type, doc_number, applied_condition_chain?}
#   contract_context   → {contract_ref, rule_id, root_cause_category?}
#   promotion_context  → {promotion_ref, root_cause_category?}
#
# Real SAP integration is a separate platform track; in-repo
# fixtures in tests/conftest.py register StubGateway responses that
# mirror this shape.

def adapt_price(record: ExceptionRecord) -> Optional[PriceAnalysisData]:
    """Project PriceAdjustmentRecipe into PriceAnalysisData.

    Four source bags:
      * `record.original_event` — po_price, sap_base_price, sku,
        line_count, uom, metadata.
      * `record.enrichment_context["sap_doc_context"]` — SAP doc
        metadata (audit-bearing: doc_type, doc_number).
      * `record.enrichment_context["contract_context"]` — contract
        lookup (audit-bearing: rule_id; contextual: contract_ref).
      * `record.enrichment_context["promotion_context"]` — promotion
        master (audit-bearing: root_cause_category; contextual:
        promotion_ref).

    Returns None when any audit-bearing gateway field is absent —
    composer routes to AUDIT_CONTEXT_MISSING.
    """
    event = record.original_event or {}
    ctx = record.enrichment_context or {}
    sap_doc = ctx.get("sap_doc_context") or {}
    contract = ctx.get("contract_context") or {}
    promotion = ctx.get("promotion_context") or {}

    doc_type = sap_doc.get("doc_type")
    doc_number = sap_doc.get("doc_number")
    rule_id = contract.get("rule_id")
    root_cause_category = (
        promotion.get("root_cause_category")
        or contract.get("root_cause_category")
    )
    if not doc_type or not doc_number or not rule_id or not root_cause_category:
        return None

    po_price = _as_float(event.get("po_price"))
    sap_base_price = _as_float(event.get("sap_base_price"))
    if sap_base_price <= 0:
        return None
    metadata = event.get("metadata") or {}
    total_quantity = _as_float(
        metadata.get("total_quantity"),
        default=_as_float(event.get("line_count"), default=1.0),
    )
    uom = str(
        sap_doc.get("uom")
        or metadata.get("uom")
        or "EA"
    )
    variance_amount = round(sap_base_price - po_price, 4)
    variance_pct = round(variance_amount / sap_base_price, 6)
    total_at_risk = round(variance_amount * total_quantity, 2)

    try:
        return PriceAnalysisData(
            erp_unit_price=sap_base_price,
            po_unit_price=po_price,
            variance_amount=variance_amount,
            variance_pct=variance_pct,
            total_at_risk=total_at_risk,
            total_quantity=total_quantity,
            uom=uom,
            doc_type=str(doc_type),
            doc_number=str(doc_number),
            sku=str(event.get("sku") or metadata.get("sku")
                    or sap_doc.get("sku") or ""),
            rule_id=str(rule_id),
            root_cause_category=str(root_cause_category),
            contract_ref=contract.get("contract_ref"),
            promotion_ref=promotion.get("promotion_ref"),
            material_desc=sap_doc.get("material_desc")
            or metadata.get("material_desc"),
            order_date=sap_doc.get("order_date")
            or metadata.get("order_date"),
        )
    except (TypeError, ValueError):
        return None


# ─── BackOrder adapter (T3) ─────────────────────────────────────────────
#
# BackOrder is the most multi-source adapter shipped so far:
#   * recipe-input fields (ordered_qty, available_qty, unit_price, uom)
#     come from `event.metadata`.
#   * recipe-output fields (gap_qty, gap_pct, at_risk, resolution_options)
#     come from `record.resolution_data` (synthesised when empty).
#   * gateway audit-bearing fields (primary_dc, atp_date) come from
#     `record.enrichment_context["inventory_snapshot"]`.
#   * conditional gateway fields (alternate_warehouses, substitutes,
#     production, inbound_po) come from the same gateway snapshot;
#     the composer enforces them only when `resolved_action` matches
#     the registry's `depends_on` predicate.

def _warehouse_info(payload: Optional[Dict[str, Any]]) -> Optional[WarehouseInfo]:
    if not payload:
        return None
    plant = payload.get("plant")
    qty = payload.get("qty")
    if plant is None or qty is None:
        return None
    try:
        return WarehouseInfo(
            plant=str(plant),
            name=str(payload.get("name") or ""),
            region=str(payload.get("region") or ""),
            qty=_as_float(qty),
        )
    except (TypeError, ValueError):
        return None


def _alternate_warehouse(payload: Dict[str, Any]) -> Optional[AlternateWarehouse]:
    try:
        return AlternateWarehouse(
            plant=str(payload.get("plant", "")),
            name=str(payload.get("name") or ""),
            region=str(payload.get("region") or ""),
            qty=_as_float(payload.get("qty")),
            eta_days=int(payload.get("eta_days") or 0),
            freight_delta_per_unit=_as_float(payload.get("freight_delta_per_unit")),
            freight_delta_total=_as_float(payload.get("freight_delta_total")),
        )
    except (TypeError, ValueError):
        return None


def _substitute(payload: Dict[str, Any]) -> Optional[SubstituteSKU]:
    try:
        return SubstituteSKU(
            sku=str(payload.get("sku", "")),
            description=str(payload.get("description") or ""),
            available_qty=_as_float(payload.get("available_qty")),
            price_delta_pct=_as_float(payload.get("price_delta_pct")),
            acceptance_rate=_as_float(payload.get("acceptance_rate")),
            source=str(payload.get("source") or ""),
            priority=int(payload.get("priority") or 0),
        )
    except (TypeError, ValueError):
        return None


def _inbound(payload: Optional[Dict[str, Any]]) -> Optional[InboundOrder]:
    if not payload:
        return None
    try:
        return InboundOrder(
            qty=_as_float(payload.get("qty")),
            date=payload.get("date"),
            eta=payload.get("eta"),
            po_num=payload.get("po_num"),
        )
    except (TypeError, ValueError):
        return None


def _resolution_option(payload: Dict[str, Any]) -> Optional[ResolutionOption]:
    try:
        scores_raw = payload.get("scores") or {}
        scores = ResolutionOptionScores(
            service=_as_float(scores_raw.get("service")),
            revenue=_as_float(scores_raw.get("revenue")),
            logistics=_as_float(scores_raw.get("logistics")),
            preference=_as_float(scores_raw.get("preference")),
        )
        return ResolutionOption(
            id=str(payload.get("id", "")),
            type=str(payload.get("type", "")),
            title=str(payload.get("title") or ""),
            description=str(payload.get("description") or ""),
            composite_score=_as_float(payload.get("composite_score")),
            scores=scores,
            sap_steps=[str(s) for s in (payload.get("sap_steps") or [])],
        )
    except (TypeError, ValueError):
        return None


def _synthesize_back_order_outputs(record: ExceptionRecord) -> Dict[str, Any]:
    """Run BackOrderResolutionRecipe synthetically on the explain /
    shadow-gated path. Recipe is pure — no side effects."""
    event = record.original_event or {}
    metadata = event.get("metadata") or {}
    ordered_qty = _as_float(metadata.get("ordered_qty"))
    available_qty = _as_float(metadata.get("available_qty"))
    if ordered_qty <= 0:
        return {}
    enrichment = (record.enrichment_context or {}).get("inventory_snapshot") or {}
    try:
        return resolve_back_order(
            order_id=str(event.get("order_id", "")),
            sku=str(event.get("sku") or metadata.get("sku") or ""),
            ordered_qty=ordered_qty,
            available_qty=available_qty,
            unit_price=_as_float(metadata.get("unit_price")),
            uom=str(metadata.get("uom") or "CS"),
            severe_gap_pct=BACK_ORDER_SEVERE_GAP_PCT,
            alternate_warehouses=enrichment.get("alternate_warehouses"),
            substitutes=enrichment.get("substitutes"),
        )
    except (TypeError, ValueError):
        return {}


def adapt_back_order(record: ExceptionRecord) -> Optional[BackOrderAnalysisData]:
    """Project BackOrderResolutionRecipe into BackOrderAnalysisData.

    Three source bags:
      * `event.metadata` — recipe inputs (ordered_qty, available_qty,
        unit_price, uom).
      * `record.enrichment_context["inventory_snapshot"]` — gateway
        snapshot (primary_dc + atp_date, plus conditional
        alternate_warehouses / substitutes / production / inbound_po).
      * `record.resolution_data` — recipe-computed gap / at_risk /
        resolution_options (synthesised if empty).

    Returns None when:
      * No primary_dc snapshot in enrichment_context (audit-bearing
        gateway field missing → composer routes to AUDIT_CONTEXT_MISSING).
      * Recipe input metadata is unusable (ordered_qty <= 0).
    """
    event = record.original_event or {}
    metadata = event.get("metadata") or {}
    ordered_qty = _as_float(metadata.get("ordered_qty"))
    if ordered_qty <= 0:
        return None

    enrichment = (record.enrichment_context or {}).get("inventory_snapshot") or {}
    primary_dc = _warehouse_info(enrichment.get("primary_dc"))
    if primary_dc is None:
        return None
    atp_date = enrichment.get("atp_date")
    if not atp_date:
        return None

    outputs = record.resolution_data or {}
    if not outputs.get("recommended_action") and outputs.get("status") != "COMPLETE":
        outputs = _synthesize_back_order_outputs(record)

    available_qty = _as_float(metadata.get("available_qty"))
    unit_price = _as_float(metadata.get("unit_price"))
    uom = str(metadata.get("uom") or "")

    raw_options = outputs.get("resolution_options") or []
    resolution_options = [
        opt for opt in (_resolution_option(o) for o in raw_options) if opt
    ]
    raw_alternates = enrichment.get("alternate_warehouses") or []
    alternates = [
        alt for alt in (_alternate_warehouse(a) for a in raw_alternates) if alt
    ]
    raw_substitutes = enrichment.get("substitutes") or []
    substitutes = [
        sub for sub in (_substitute(s) for s in raw_substitutes) if sub
    ]

    try:
        return BackOrderAnalysisData(
            ordered_qty=ordered_qty,
            available_qty=available_qty,
            gap_qty=_as_float(outputs.get("gap_qty"), default=ordered_qty - available_qty),
            gap_pct=_as_float(outputs.get("gap_pct"),
                              default=(ordered_qty - available_qty) / ordered_qty),
            unit_price=unit_price,
            uom=uom,
            at_risk=_as_float(outputs.get("at_risk"),
                              default=(ordered_qty - available_qty) * unit_price),
            atp_date=str(atp_date),
            primary_dc=primary_dc,
            resolution_options=resolution_options,
            alternate_warehouses=alternates,
            substitutes=substitutes,
            production=_inbound(enrichment.get("production")),
            inbound_po=_inbound(enrichment.get("inbound_po")),
        )
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# EmailOrderEntryRecipe → email_order_entry_analysis (ADR-034 Phase B)
# --------------------------------------------------------------------------


def _floor_status_from_record(
    record: ExceptionRecord,
) -> EmailOrderEntryFloorStatus:
    """Project the four "non-disable-able floor" booleans into the
    typed Pydantic model.

    Source priority (Pillar 1):
      1. `record.enrichment_context.{<gateway_response_key>}.<flag>` —
         the email_intake gateway's authoritative response.
      2. `record.original_event.metadata.non_disableable_floor.<flag>`
         — defensive fallback when the gateway response is empty (e.g.
         direct-recipe-call paths in unit tests, or a soft gateway
         failure handled upstream).
      3. False — conservative default if neither source is present.

    Each flag has a corresponding gateway operation; the mapping is
    declared on the recipe spec in `recipes/registry.py`.
    """
    enrichment = record.enrichment_context or {}
    metadata = (record.original_event or {}).get("metadata") or {}
    fallback = metadata.get("non_disableable_floor") or {}
    if not isinstance(fallback, dict):
        fallback = {}

    def _resolve(gateway_key: str, flag_key: str, fallback_key: str) -> bool:
        gw = enrichment.get(gateway_key)
        if isinstance(gw, dict) and flag_key in gw:
            return bool(gw.get(flag_key))
        return bool(fallback.get(fallback_key, False))

    return EmailOrderEntryFloorStatus(
        sender_authorized=_resolve(
            "sender_auth_context", "sender_authorized", "sender_authorized",
        ),
        customer_resolved=_resolve(
            "customer_resolution_context", "customer_resolved", "customer_resolved",
        ),
        duplicate_po_clear=_resolve(
            "duplicate_po_pre_check_context", "duplicate_po_clear", "duplicate_po_clear",
        ),
        credit_clear=_resolve(
            "credit_check_context", "credit_clear", "credit_clear",
        ),
    )


def _eoe_from_outputs(
    outputs: Dict[str, Any], floor_status: EmailOrderEntryFloorStatus,
) -> Optional[EmailOrderEntryAnalysisData]:
    classification = outputs.get("classification")
    autonomy_level = outputs.get("autonomy_level")
    if classification is None or autonomy_level is None:
        return None
    failures = outputs.get("validation_failures") or []
    if not isinstance(failures, list):
        failures = []
    breaches = outputs.get("floor_breaches") or []
    if not isinstance(breaches, list):
        breaches = []
    try:
        return EmailOrderEntryAnalysisData(
            composite_confidence=float(outputs.get("composite_confidence") or 0.0),
            classification=classification,
            recommended_action=str(outputs.get("recommended_action", "")),
            autonomy_level=autonomy_level,
            validation_failures=[str(f) for f in failures],
            floor_breaches=[str(b) for b in breaches],
            reject_reason_code=outputs.get("reject_reason_code"),
            floor_status=floor_status,
            notification_template=outputs.get("notification_template"),
        )
    except (TypeError, ValueError):
        return None


def adapt_email_order_entry(
    record: ExceptionRecord,
) -> Optional[EmailOrderEntryAnalysisData]:
    """Project an EmailOrderEntryRecipe resolution into
    EmailOrderEntryAnalysisData (ADR-034 Phase B).

    Three-path structure mirroring `adapt_edi_mismatch`:
      1. Recipe output present (GREEN path): project directly.
      2. Shadow-gated (YELLOW/RED): synthesise the recipe call from
         event metadata so the operator still sees the agent's
         analysis on the audit-bearing surface.
      3. Routing/structural error: recipe returns None / FAILED →
         adapter returns None and the composer routes to
         AUDIT_CONTEXT_MISSING per Pillar 2.

    Floor evidence (the four non-disable-able booleans) is sourced
    from `record.enrichment_context.{sender_auth_context,
    customer_resolution_context, duplicate_po_pre_check_context,
    credit_check_context}` — the email_intake gateway responses
    declared on the recipe spec — and falls back to
    `event.metadata.non_disableable_floor` defensively.
    """
    floor_status = _floor_status_from_record(record)
    outputs = record.resolution_data or {}
    event = record.original_event or {}
    status = outputs.get("status")

    if status and status != "FAILED":
        projection = _eoe_from_outputs(outputs, floor_status)
        if projection is not None:
            return projection

    # Synthetic call on the shadow-gated path. We hand the recipe the
    # exact same inputs validate_types.py would have if shadow had
    # returned GREEN — sourced from event.metadata so we don't need
    # the recipe to have actually run.
    metadata = event.get("metadata") or {}
    floor_meta = metadata.get("non_disableable_floor") or {}
    if not isinstance(floor_meta, dict):
        floor_meta = {}
    # Prefer gateway-resolved floor booleans on the synthetic path too —
    # the recipe consumes whatever the orchestration layer would have
    # passed it, which prefers gateway over metadata.
    synthetic_floor: Dict[str, bool] = {}
    enrichment = record.enrichment_context or {}
    gw_keys = (
        ("sender_auth_context", "sender_authorized", "sender_authorized"),
        ("customer_resolution_context", "customer_resolved", "customer_resolved"),
        ("duplicate_po_pre_check_context", "duplicate_po_clear", "duplicate_po_clear"),
        ("credit_check_context", "credit_clear", "credit_clear"),
    )
    for gw_key, flag, recipe_key in gw_keys:
        gw = enrichment.get(gw_key)
        if isinstance(gw, dict) and flag in gw:
            synthetic_floor[recipe_key] = bool(gw.get(flag))
        elif recipe_key in floor_meta:
            synthetic_floor[recipe_key] = bool(floor_meta.get(recipe_key, False))
    failures = metadata.get("validation_failures") or []
    if not isinstance(failures, list):
        failures = []
    try:
        synthetic = classify_email_order_entry(
            order_id=str(event.get("order_id", "")),
            customer_id=str(event.get("retailer_id") or ""),
            composite_confidence=float(metadata.get("composite_confidence") or 0.0),
            validation_failures=[str(f) for f in failures],
            non_disableable_floor=synthetic_floor,
            autonomy_levels=EMAIL_ORDER_ENTRY_AUTONOMY_LEVELS,
            threshold_auto_approve=EMAIL_ORDER_AUTO_APPROVE_CONFIDENCE,
            threshold_review_band_low=EMAIL_ORDER_REVIEW_BAND_LOW,
            threshold_auto_correct=EMAIL_ORDER_AUTO_CORRECT_CONFIDENCE,
            reject_reason_code=metadata.get("reject_reason_code"),
        )
    except (TypeError, ValueError):
        return None
    if synthetic.get("status") == "FAILED":
        return None
    return _eoe_from_outputs(synthetic, floor_status)


def adapt_email_source(
    record: ExceptionRecord,
) -> Optional[EmailSourceData]:
    """Project the email source-of-truth substrate (ADR-034 Phase G).

    SECONDARY adapter on `EmailOrderEntryRecipe.py` (same attestation
    target as the primary `email_order_entry_analysis` — both are
    surfaced on the Exception Queue detail page for an email-channel
    order). Sources from
    `record.enrichment_context["email_source_context"]` — the
    `email_intake/fetch_message` gateway response.

    Returns None when the gateway response is absent (e.g. an
    EDI-channel record where this adapter's primary doesn't run, or
    a soft gateway failure). The composer treats secondary `None`
    as structural omission (the section simply doesn't mount).
    """
    enrichment = record.enrichment_context or {}
    src = enrichment.get("email_source_context")
    if not isinstance(src, dict) or not src:
        return None

    raw_attachments = src.get("attachment_manifest") or []
    if not isinstance(raw_attachments, list):
        raw_attachments = []
    manifest: list[EmailAttachmentManifestEntry] = []
    for raw in raw_attachments:
        if not isinstance(raw, dict):
            continue
        try:
            manifest.append(EmailAttachmentManifestEntry(
                name=str(raw.get("name") or ""),
                mime_type=str(raw.get("mime_type") or ""),
                bytes=int(raw.get("bytes") or 0),
            ))
        except (TypeError, ValueError):
            continue

    try:
        return EmailSourceData(
            from_address=str(src.get("from_address") or ""),
            received_at=str(src.get("received_at") or ""),
            subject=str(src.get("subject") or ""),
            body_hash=str(src.get("body_hash") or ""),
            attachment_manifest=manifest,
            body_excerpt=src.get("body_excerpt"),
            source_email_id=src.get("source_email_id"),
        )
    except (TypeError, ValueError):
        return None


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
    "PalletAlignmentRecipe.py": ("pallet_analysis", adapt_pallet),
    "DuplicatePORecipe.py": ("duplicate_detection", adapt_duplicate),
    "BackOrderResolutionRecipe.py": ("backorder_analysis", adapt_back_order),
    "PriceAdjustmentRecipe.py": ("price_analysis", adapt_price),
    "EmailOrderEntryRecipe.py": (
        "email_order_entry_analysis", adapt_email_order_entry,
    ),
}


# Recipe-name → list of (field, adapter) pairs for derived projections.
# The composer enforces audit-bearing coverage against the PRIMARY
# (`ANALYSIS_ADAPTERS`); secondaries are best-effort projections that
# share the primary's attestation target. Use this when a single
# gateway payload powers multiple UI sections — e.g. matched_po_details
# drives both `duplicate_detection` (primary) and `order_comparison`
# (secondary, registry rationale: "same attestation target").
SECONDARY_ANALYSIS_ADAPTERS: Dict[
    str, Tuple[Tuple[str, Callable[[ExceptionRecord], Any]], ...]
] = {
    "DuplicatePORecipe.py": (
        ("order_comparison", adapt_order_comparison),
    ),
    # ADR-034 Phase G — the email source-of-truth substrate shares the
    # primary's attestation target (the operator authorising an email-
    # channel order needs both the agent's recommendation AND the
    # source-of-truth for the order they're acting on).
    "EmailOrderEntryRecipe.py": (
        ("email_source", adapt_email_source),
    ),
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
    "PALLET_CONFIG": "PalletAlignmentRecipe.py",
    "DUPLICATE_PO": "DuplicatePORecipe.py",
    "BACK_ORDER": "BackOrderResolutionRecipe.py",
    "CONTRACTUAL_CORRECTION": "PriceAdjustmentRecipe.py",
    "EMAIL_ORDER_ENTRY": "EmailOrderEntryRecipe.py",
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
