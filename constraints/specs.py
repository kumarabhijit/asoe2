from __future__ import annotations

from typing import Literal, Optional, List
from pydantic import BaseModel, Field, ConfigDict

AllowedIntent = Literal[
    "CONTRACTUAL_CORRECTION",
    "CREDIT_BLOCK",
    "MASS_PRICING_ERROR",
    "DUPLICATE_PO",
    "PRICE_HOLD_RELEASE",
    "EDI_MISMATCH",
    "BACK_ORDER",
    "OVER_MAX",
    "MIN_ORDER_QTY",
    "PALLET_CONFIG",
    "DELIVERY_DELAY",
    "MANUAL_ORDER_INTAKE",
]

AllowedShadowStatus = Literal["GREEN", "YELLOW", "RED"]

AllowedRecipeName = Literal[
    "PriceAdjustmentRecipe.py",
    "CreditHoldReleaseRecipe.py",
    "DuplicatePORecipe.py",
    "PriceHoldReleaseRecipe.py",
    "EdiMismatchRecipe.py",
    "BackOrderResolutionRecipe.py",
    "OverMaxTrimRecipe.py",
    "MOQRoundUpRecipe.py",
    "PalletAlignmentRecipe.py",
    "DeliveryDelayResolutionRecipe.py",
    "EmailOrderEntryRecipe.py",
]

# EDI 850 line-mismatch sub_type vocabulary. PRICE_MISMATCH is intentionally
# absent — those events route to CONTRACTUAL_CORRECTION / PriceAdjustmentRecipe.py
# at classifier time, keeping pricing a single source of truth (CLAUDE.md §1).
AllowedEdiMismatchSubType = Literal[
    "SKU_MISMATCH",
    "QTY_MISMATCH",
    "UOM_MISMATCH",
    "SHIP_TO_MISMATCH",
]

# Recipe output classification for EdiMismatchRecipe. Machine-consumed by
# apply_effects / executor status mapping. CLAUDE.md §3 requires Literal gating.
AllowedEdiMismatchClassification = Literal[
    "HARD_REJECT",
    "REVIEW",
    "ESCALATE",
]

# Recipe output action for PriceHoldReleaseRecipe. Machine-consumed by
# apply_effects (OMS update_hold_flag payload) and trace pipeline.
AllowedPriceHoldAction = Literal[
    "AUTO_RELEASE",
    "ESCALATE",
    "HARD_BLOCK",
]

AllowedResolutionAction = Literal[
    "BLOCK_AND_NOTIFY",
    "MERGE",
    "SUPERSEDE",
    "ALLOW_BOTH",
    "ESCALATE",
    "REQUEST_BUYER_CONFIRMATION",
    # ADR-034 — EmailOrderEntryRecipe outputs.
    "ONE_CLICK_APPROVE",
    "STANDARD_REVIEW",
    "LOW_CONFIDENCE_FLAG",
    "AUTO_CORRECT",
    "REQUEST_CLARIFICATION",
    "REJECT",
]

# Controlled vocabulary for categorizing an override's justification.
#
# Per ADR-033 V1 §1, this Literal is the *union* of every per-intent
# vocabulary. Pre-existing legacy codes are lowercase; per-intent
# vocabularies curated under the ADR-033 lifecycle use SCREAMING_SNAKE_CASE.
# The two coexist: a value's case identifies which vocabulary it belongs to.
#
# Per-intent narrowing happens at the API surface — see INTENT_REASON_TAGS
# below and `/api/v1/health.allowed_override_reason_tags_by_intent`. The
# /disposition endpoint validates the chosen reason against the *per-intent*
# set, not against this global Literal.
#
# Adding a new per-intent vocabulary: extend this Literal with the new codes,
# add a tuple constant (`_<INTENT>_REASON_TAGS`), and assign it into
# INTENT_REASON_TAGS[<INTENT>]. Tests in `tests/test_constraints.py` enforce
# that every intent's set ends with `OTHER` (or legacy `other`) and that
# every tuple entry is a member of this Literal.
AllowedOverrideReasonTag = Literal[
    # Legacy / global fallback codes (lowercase). Predate the
    # SCREAMING_SNAKE_CASE convention; retained for intents whose
    # vocabulary has not yet been curated.
    "customer_concession",
    "contract_stale",
    "data_error",
    "policy_exception",
    "agent_misclassification",
    "other",
    # DUPLICATE_PO curated codes (ADR-033 §A). SCREAMING_SNAKE_CASE per
    # the lifecycle (ADR-033 §C.2). `OTHER` is the workflow-safety
    # fallback and is mandatory in every per-intent set.
    "INTENTIONAL_REORDER",
    "AMENDED_PO",
    "BLANKET_RELEASE",
    "SYSTEM_RETRY_VALID",
    "DIFFERENT_SHIP_TO",
    "CONFIRMED_DUPLICATE",
    "PARTIAL_OVERLAP",
    "OTHER",
]

# Per-intent override-reason vocabulary (ADR-033).
#
# The /disposition endpoint validates the chosen reason against the
# *per-intent* set when the record carries a known intent, and falls
# back to `_GLOBAL_REASON_TAGS` for FAILED-lifecycle records or any
# intent without a curated vocabulary. The UI reads the per-intent map
# from `/api/v1/health.allowed_override_reason_tags_by_intent` and
# narrows its chooser accordingly.
#
# Lifecycle (ADR-033 §C):
#   1. Source: product + compliance + (where applicable) ML jointly draft
#      candidate codes. Calibration use case considered explicitly.
#   2. Constraints: every set must include `OTHER`/`other` as the last
#      entry; new codes are SCREAMING_SNAKE_CASE; intent-meaningful.
#   3. Storage: tuple literal here (no dynamic generation — the literal
#      is the contract).
#   4. API: /health surfaces the per-intent map automatically.
#   5. UI: OverrideChooserDialog reads the per-intent set; cluster
#      mapping is a UI-side constant.
#   6. Tests: vocabulary-sync test in tests/test_constraints.py.
#   7. Audit: changes are versioned in git; no runtime config surface
#      (reason vocabulary is product policy, not tenant config).
_GLOBAL_REASON_TAGS: tuple[str, ...] = (
    "customer_concession",
    "contract_stale",
    "data_error",
    "policy_exception",
    "agent_misclassification",
    "other",
)

# DUPLICATE_PO — first curated per-intent vocabulary (ADR-033 §A).
# These 8 codes align with `docs/specs/duplicate-po/calibration-methodology.md`
# so the eventual calibration loop (ADR-032) can consume override-reason
# tuples directly as labeled training data — no schema bridge or
# vocabulary translation needed.
_DUPLICATE_PO_REASON_TAGS: tuple[str, ...] = (
    "INTENTIONAL_REORDER",     # buyer genuinely placed a second order
    "AMENDED_PO",              # incoming PO is a revision, not a duplicate
    "BLANKET_RELEASE",         # release against a blanket umbrella PO
    "SYSTEM_RETRY_VALID",      # middleware retransmit was intentional/valid
    "DIFFERENT_SHIP_TO",       # same PO# routed to different destinations
    "CONFIRMED_DUPLICATE",     # agent was correct; analyst confirms
    "PARTIAL_OVERLAP",         # some lines overlap but the order is distinct
    "OTHER",                   # free-text required (ADR-033 §D)
)

INTENT_REASON_TAGS: dict[str, tuple[str, ...]] = {
    intent: _GLOBAL_REASON_TAGS for intent in AllowedIntent.__args__  # type: ignore[attr-defined]
}
INTENT_REASON_TAGS["DUPLICATE_PO"] = _DUPLICATE_PO_REASON_TAGS


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
