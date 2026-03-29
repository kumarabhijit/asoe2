from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GuidanceRegexBackend:
    def intent_regex(self) -> str:
        return r"CONTRACTUAL_CORRECTION|CREDIT_BLOCK|MASS_PRICING_ERROR|DUPLICATE_PO"

    def shadow_verdict_regex(self) -> str:
        return r"GREEN|YELLOW|RED"

    def recipe_name_regex(self) -> str:
        return r"PriceAdjustmentRecipe\.py|CreditHoldReleaseRecipe\.py|DuplicatePORecipe\.py"

    def resolution_action_regex(self) -> str:
        return r"BLOCK_AND_NOTIFY|MERGE|SUPERSEDE|ALLOW_BOTH|ESCALATE|REQUEST_BUYER_CONFIRMATION"
