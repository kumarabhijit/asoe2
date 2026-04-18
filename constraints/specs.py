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

# Controlled vocabulary for categorizing an override's justification. Kept
# deliberately small and intent-agnostic so clustering downstream is stable;
# free-text notes on OverrideRequest capture the specifics. ML retraining
# consumes (intent, recommended_action, chosen_action, reason_tag) tuples.
AllowedOverrideReasonTag = Literal[
    "customer_concession",
    "contract_stale",
    "data_error",
    "policy_exception",
    "agent_misclassification",
    "other",
]

# Per-intent override-reason vocabulary (Phase 3 follow-up — Option A).
#
# Framework only: every intent currently points at the full global
# AllowedOverrideReasonTag set, so clustering by (intent, reason_tag)
# produces the same signal the global vocab gives today. The value-add
# of per-intent tags arrives in a follow-up when product / compliance
# supplies curated categories — e.g. PRICE_MISMATCH might narrow to
# {customer_concession, contract_stale, promo_window, data_error, other}
# and DUPLICATE_PO to
# {confirmed_separate_orders, customer_intent, data_error, other}.
# Swapping those in is a DATA change — no schema, API, or UI work.
#
# ``other`` is mandatory in every set so operators always have a fallback
# when the real reason doesn't fit a listed category (prevents workflow
# dead-ends while the vocabulary is being curated).
#
# Falls-back behavior: the /disposition endpoint uses the per-intent set
# when the record carries a known intent, and the global set otherwise
# (FAILED lifecycle records and any unlisted intent). The UI reads the
# map from /api/v1/health.allowed_override_reason_tags_by_intent and
# narrows its chooser accordingly.
_GLOBAL_REASON_TAGS: tuple[str, ...] = (
    "customer_concession",
    "contract_stale",
    "data_error",
    "policy_exception",
    "agent_misclassification",
    "other",
)
INTENT_REASON_TAGS: dict[str, tuple[str, ...]] = {
    intent: _GLOBAL_REASON_TAGS for intent in AllowedIntent.__args__  # type: ignore[attr-defined]
}


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
