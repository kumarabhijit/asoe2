from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Tuple

from contracts.models import GatewayDependency, GatewayEffect
from recipes.CreditHoldReleaseRecipe import release_credit_hold
from recipes.DuplicatePORecipe import detect_duplicate_po
from recipes.PriceAdjustmentRecipe import execute_price_correction


@dataclass(frozen=True)
class RecipeSpec:
    name: str
    func: Callable[..., Dict[str, Any]]
    required_params: tuple[str, ...]
    allowed_intents: tuple[str, ...]
    # Gateway integration — declared, resolved by orchestration (never by recipes)
    dependencies: Tuple[GatewayDependency, ...] = ()
    effects: Tuple[GatewayEffect, ...] = ()


REGISTRY = {
    "PriceAdjustmentRecipe.py": RecipeSpec(
        name="PriceAdjustmentRecipe.py",
        func=execute_price_correction,
        required_params=("order_id", "line_item", "requested_price", "erp_context"),
        allowed_intents=("CONTRACTUAL_CORRECTION",),
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
}


def get_recipe(name: str) -> RecipeSpec:
    if name not in REGISTRY:
        raise KeyError(f"Unknown recipe: {name}")
    return REGISTRY[name]
