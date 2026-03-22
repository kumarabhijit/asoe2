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
    ),
}


def get_recipe(name: str) -> RecipeSpec:
    if name not in REGISTRY:
        raise KeyError(f"Unknown recipe: {name}")
    return REGISTRY[name]
