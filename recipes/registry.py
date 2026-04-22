from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Tuple

from contracts.models import GatewayDependency, GatewayEffect
from recipes.BackOrderResolutionRecipe import resolve_back_order
from recipes.CreditHoldReleaseRecipe import release_credit_hold
from recipes.DeliveryDelayResolutionRecipe import resolve_delivery_delay
from recipes.DuplicatePORecipe import detect_duplicate_po
from recipes.EdiMismatchRecipe import detect_edi_mismatch
from recipes.MOQRoundUpRecipe import round_up_moq
from recipes.OverMaxTrimRecipe import trim_over_max
from recipes.PalletAlignmentRecipe import align_pallets
from recipes.PriceAdjustmentRecipe import execute_price_correction
from recipes.PriceHoldReleaseRecipe import execute_price_hold_release


@dataclass(frozen=True)
class RecipeSpec:
    name: str
    func: Callable[..., Dict[str, Any]]
    required_params: tuple[str, ...]
    allowed_intents: tuple[str, ...]
    # Gateway integration — declared, resolved by orchestration (never by recipes)
    dependencies: Tuple[GatewayDependency, ...] = ()
    effects: Tuple[GatewayEffect, ...] = ()
    # V1 Foundation Guardrail #3: metadata keys this recipe expects on OrderEvent.metadata
    expected_metadata_keys: tuple[str, ...] = ()


REGISTRY = {
    "PriceAdjustmentRecipe.py": RecipeSpec(
        name="PriceAdjustmentRecipe.py",
        func=execute_price_correction,
        required_params=("order_id", "line_item", "requested_price", "erp_context"),
        allowed_intents=("CONTRACTUAL_CORRECTION",),
        # Verdict T4: SAP doc + contract + promotion gateway READS
        # populate the audit-bearing PriceAnalysisData fields. Each
        # response shape is documented in api/analysis_adapters.py
        # (adapt_price). Retiring price_analysis_gateway_gap.
        dependencies=(
            GatewayDependency(
                gateway_name="sap_doc",
                operation="lookup",
                params_from_state={
                    "order_id": "event.order_id",
                    "line_item": "event.line_item",
                },
                result_key="sap_doc_context",
            ),
            GatewayDependency(
                gateway_name="sap_contract",
                operation="lookup",
                params_from_state={
                    "order_id": "event.order_id",
                    "retailer_id": "event.retailer_id",
                    "sku": "event.sku",
                },
                result_key="contract_context",
            ),
            GatewayDependency(
                gateway_name="promotion",
                operation="lookup",
                params_from_state={
                    "order_id": "event.order_id",
                    "sku": "event.sku",
                },
                result_key="promotion_context",
            ),
        ),
    ),
    "CreditHoldReleaseRecipe.py": RecipeSpec(
        name="CreditHoldReleaseRecipe.py",
        func=release_credit_hold,
        required_params=("order_id", "requester_role", "credit_limit", "current_exposure", "authorized_roles", "exposure_tolerance"),
        allowed_intents=("CREDIT_BLOCK",),
    ),
    "DuplicatePORecipe.py": RecipeSpec(
        name="DuplicatePORecipe.py",
        func=detect_duplicate_po,
        required_params=("incoming_po_number", "customer_id", "signal_scores", "threshold_auto_block", "threshold_review_required", "threshold_soft_flag"),
        allowed_intents=("DUPLICATE_PO",),
        expected_metadata_keys=("signal_scores", "matched_po_id"),
        dependencies=(
            GatewayDependency(
                gateway_name="oms",
                operation="get_fulfillment_status",
                params_from_state={"order_id": "event.order_id"},
                result_key="fulfillment_status",
            ),
            GatewayDependency(
                gateway_name="oms",
                operation="get_matched_po_details",
                params_from_state={"order_id": "event.order_id", "customer_id": "event.retailer_id"},
                result_key="matched_po_details",
            ),
        ),
        effects=(
            GatewayEffect(
                gateway_name="buyer_notification",
                operation="send",
                params_from_output={
                    "template": "notification_template",
                    "po_number": "incoming_po_number",
                    "customer_id": "customer_id",
                },
            ),
        ),
    ),
    "PriceHoldReleaseRecipe.py": RecipeSpec(
        name="PriceHoldReleaseRecipe.py",
        func=execute_price_hold_release,
        required_params=(
            "order_id", "line_item", "po_price", "sap_base_price",
            "tolerance_pct", "hard_block_pct", "hold_status",
        ),
        allowed_intents=("PRICE_HOLD_RELEASE",),
        expected_metadata_keys=("price_hold_status",),
        dependencies=(
            GatewayDependency(
                gateway_name="oms",
                operation="get_price_hold_status",
                params_from_state={"order_id": "event.order_id"},
                result_key="price_hold_status",
            ),
        ),
        effects=(
            GatewayEffect(
                gateway_name="oms",
                operation="update_hold_flag",
                params_from_output={
                    "order_id": "order_id",
                    "action": "action",
                },
            ),
        ),
    ),
    "EdiMismatchRecipe.py": RecipeSpec(
        name="EdiMismatchRecipe.py",
        func=detect_edi_mismatch,
        required_params=(
            "order_id", "sub_type", "expected_value",
            "received_value", "autonomy_levels",
        ),
        allowed_intents=("EDI_MISMATCH",),
        expected_metadata_keys=(
            "mismatch_sub_type", "expected_value", "received_value",
        ),
        effects=(
            GatewayEffect(
                gateway_name="buyer_notification",
                operation="send",
                params_from_output={
                    "template": "notification_template",
                    "order_id": "order_id",
                },
            ),
        ),
    ),
    "BackOrderResolutionRecipe.py": RecipeSpec(
        name="BackOrderResolutionRecipe.py",
        func=resolve_back_order,
        required_params=(
            "order_id", "sku", "ordered_qty", "available_qty",
            "unit_price", "uom", "severe_gap_pct",
        ),
        allowed_intents=("BACK_ORDER",),
        expected_metadata_keys=(
            "ordered_qty", "available_qty", "unit_price",
        ),
        dependencies=(
            GatewayDependency(
                gateway_name="oms",
                operation="get_inventory_snapshot",
                params_from_state={
                    "order_id": "event.order_id",
                    "sku": "event.sku",
                },
                result_key="inventory_snapshot",
            ),
        ),
        effects=(
            GatewayEffect(
                gateway_name="buyer_notification",
                operation="send",
                params_from_output={
                    "template": "backorder_template",
                    "order_id": "order_id",
                },
            ),
        ),
    ),
    "OverMaxTrimRecipe.py": RecipeSpec(
        name="OverMaxTrimRecipe.py",
        func=trim_over_max,
        required_params=(
            "order_id", "total_ordered", "max_qty",
            "severe_exceedance_pct",
        ),
        allowed_intents=("OVER_MAX",),
        expected_metadata_keys=("max_qty", "order_lines"),
        # Verdict T5: SAP contract + block-status gateway READS
        # populate the audit-bearing OverMax fields previously
        # under overmax_gateway_gap.
        dependencies=(
            GatewayDependency(
                gateway_name="sap_contract",
                operation="lookup",
                params_from_state={
                    "order_id": "event.order_id",
                    "retailer_id": "event.retailer_id",
                },
                result_key="contract_context",
            ),
            GatewayDependency(
                gateway_name="sap_block",
                operation="lookup",
                params_from_state={
                    "order_id": "event.order_id",
                },
                result_key="block_context",
            ),
        ),
    ),
    "MOQRoundUpRecipe.py": RecipeSpec(
        name="MOQRoundUpRecipe.py",
        func=round_up_moq,
        required_params=(
            "order_id", "sku", "ordered_qty", "moq_qty",
            "unit_cost", "uom", "severe_shortfall_pct",
            "uplift_review_pct",
        ),
        allowed_intents=("MIN_ORDER_QTY",),
        expected_metadata_keys=("moq_qty", "unit_cost"),
        # Verdict T5: customer-master + contract + block-status
        # gateway READS populate the audit-bearing MOQ fields
        # previously under moq_gateway_gap.
        dependencies=(
            GatewayDependency(
                gateway_name="sap_customer_master",
                operation="lookup",
                params_from_state={
                    "retailer_id": "event.retailer_id",
                    "sku": "event.sku",
                },
                result_key="customer_master_context",
            ),
            GatewayDependency(
                gateway_name="sap_contract",
                operation="lookup",
                params_from_state={
                    "order_id": "event.order_id",
                    "retailer_id": "event.retailer_id",
                },
                result_key="contract_context",
            ),
            GatewayDependency(
                gateway_name="sap_block",
                operation="lookup",
                params_from_state={
                    "order_id": "event.order_id",
                },
                result_key="block_context",
            ),
        ),
    ),
    "PalletAlignmentRecipe.py": RecipeSpec(
        name="PalletAlignmentRecipe.py",
        func=align_pallets,
        required_params=(
            "order_id", "lines", "min_fill_pct",
            "broken_layer_fill_pct",
        ),
        allowed_intents=("PALLET_CONFIG",),
        expected_metadata_keys=("pallet_lines",),
    ),
    "DeliveryDelayResolutionRecipe.py": RecipeSpec(
        name="DeliveryDelayResolutionRecipe.py",
        func=resolve_delivery_delay,
        required_params=(
            "order_id", "planned_date", "projected_eta",
            "minor_days", "severe_days",
        ),
        allowed_intents=("DELIVERY_DELAY",),
        expected_metadata_keys=("planned_date", "projected_eta"),
        # Verdict T5: SLA contract gateway READ populates the
        # audit-bearing financial fields previously under
        # delivery_delay_financial_gap (at_risk, sla_deadline).
        dependencies=(
            GatewayDependency(
                gateway_name="sla_contract",
                operation="lookup",
                params_from_state={
                    "order_id": "event.order_id",
                    "retailer_id": "event.retailer_id",
                },
                result_key="sla_contract_context",
            ),
        ),
    ),
}


def get_recipe(name: str) -> RecipeSpec:
    if name not in REGISTRY:
        raise KeyError(f"Unknown recipe: {name}")
    return REGISTRY[name]
