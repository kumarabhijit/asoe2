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

from api.schemas import EdiMismatchAnalysisData, PriceHoldAnalysisData
from api.store import ExceptionRecord
from contracts.policy import (
    EDI_MISMATCH_AUTONOMY_LEVELS,
    PRICE_HOLD_HARD_BLOCK_PCT,
    PRICE_HOLD_TOLERANCE_PCT,
)
from recipes.EdiMismatchRecipe import detect_edi_mismatch
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
}


# Intent → recipe-name fallback. Used by the endpoint when
# `record.selected_recipe` is None (shadow_audit gated before
# select_recipe ran). `record.intent` is always set by
# classify_intent which runs earlier, so this gives the adapter
# enough signal to produce a synthetic projection.
INTENT_TO_RECIPE_NAME: Dict[str, str] = {
    "PRICE_HOLD_RELEASE": "PriceHoldReleaseRecipe.py",
    "EDI_MISMATCH": "EdiMismatchRecipe.py",
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
