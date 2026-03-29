from __future__ import annotations

from typing import Literal, Optional, List
from pydantic import BaseModel, Field, ConfigDict

AllowedIntent = Literal[
    "CONTRACTUAL_CORRECTION",
    "CREDIT_BLOCK",
    "MASS_PRICING_ERROR",
    "DUPLICATE_PO",
]

AllowedShadowStatus = Literal["GREEN", "YELLOW", "RED"]

AllowedRecipeName = Literal[
    "PriceAdjustmentRecipe.py",
    "CreditHoldReleaseRecipe.py",
    "DuplicatePORecipe.py",
]

AllowedResolutionAction = Literal[
    "BLOCK_AND_NOTIFY",
    "MERGE",
    "SUPERSEDE",
    "ALLOW_BOTH",
    "ESCALATE",
    "REQUEST_BUYER_CONFIRMATION",
]


class IntentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: AllowedIntent
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: Optional[str] = None


class ShadowDecisionSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: AllowedShadowStatus
    reasons: List[str]
    policy_hits: List[str] = Field(default_factory=list)


class RecipeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipe_name: AllowedRecipeName
