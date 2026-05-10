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
    # SCREAMING_SNAKE_CASE convention; retained for grandfathered
    # audit-log rows on read (ADR-033 §C / Phase 5.3 — hash chain
    # must not be retroactively invalidated). On forward writes the
    # validator narrows to the per-intent set; once every intent is
    # curated these legacy entries are read-only.
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
    # B1 CONTRACTUAL_CORRECTION (panel 2026-05-10).
    "CONTRACT_STALE_PRICE",
    "PROMO_WINDOW_EXPIRED",
    "SAP_MASTER_PRICE_ERROR",
    "CUSTOMER_CONCESSION_LOW",
    "CUSTOMER_CONCESSION_HIGH",
    "BAND_REVIEW_OVERRIDDEN",
    # B2 CREDIT_BLOCK.
    "FINANCE_RELEASED_ONE_TIME",
    "CREDIT_LINE_ADJUSTED",
    "CUSTOMER_PREPAY_ARRANGED",
    "RISK_ACCEPTED_OPS",
    # B3 MANUAL_ORDER_INTAKE.
    "EXTRACTION_LOW_CONFIDENCE",
    "FIELD_MISSING_BUYER_CLARIFIED",
    "FIELD_MISSING_BUYER_UNREACHABLE",
    "UNSUPPORTED_FORMAT",
    "MULTI_PO_SPLIT",
    # B4 EDI_MISMATCH.
    "EDI_PARTNER_MAPPING_ERROR",
    "CUSTOMER_EDI_MISCONFIGURED",
    "SAP_MASTER_DIFF",
    "TOLERANCE_BAND_ACCEPTED",
    "RESUBMITTED_TO_PARTNER",
    # B5 BACK_ORDER.
    "ALTERNATE_WAREHOUSE_APPROVED",
    "SUBSTITUTE_SKU_APPROVED",
    "CUSTOMER_ACCEPTS_DELAY",
    "PARTIAL_FULFILMENT_ACCEPTED",
    "CANCELLATION_ACCEPTED",
    # B6 PRICE_HOLD_RELEASE.
    "OPERATIONS_AUTHORISED",
    "FINANCE_AUTHORISED",
    "CONTRACT_VERIFIED",
    "BULK_RELEASE_APPROVED",
    # B7 OVER_MAX.
    "TRIM_APPLIED",
    "OVER_MAX_ALLOWED_CUSTOMER_REQUEST",
    "OVER_MAX_ALLOWED_SEASONAL",
    "CUSTOMER_MAX_STALE",
    # B8 MIN_ORDER_QTY.
    "ROUND_UP_APPLIED",
    "OPT_OUT_VIOLATION_ROUND_UP",
    # B9 PALLET_CONFIG.
    "TIE_LAYER_OVERRIDDEN",
    "PALLET_HEIGHT_OVERRIDDEN",
    "FREIGHT_CLASS_OVERRIDDEN",
    "CASE_COUNT_OVERRIDDEN",
    # B10 DELIVERY_DELAY.
    "CUSTOMER_ACCEPTS_NEW_DATE",
    "EXPEDITE_APPROVED",
    "PARTIAL_SPLIT_SHIPMENT",
    # B11 MASS_PRICING_ERROR (no recipe; admin-release path).
    "BULK_DATA_ERROR_CONFIRMED",
    "BULK_PROMO_EXTENSION",
    "MASS_CONCESSION_HONOURED",
    "FALSE_POSITIVE",
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

# Canonical names below are sourced from the CPG SME panel
# 2026-05-10 — see `docs/workshops/reason-tags/consolidated-panel-2026-05-10.md`.
# Each tag's audit-bearing required fields, learning-signal
# annotation, and freshness window are documented there; this
# module records the **vocabulary** only (the audit / ML
# metadata stays in the panel doc until ADR-033's lifecycle
# wires them into the registry).

# B1 — CONTRACTUAL_CORRECTION (PriceAdjustmentRecipe).
# Panel ratified `_LOW` / `_HIGH` split on CUSTOMER_CONCESSION
# (§A6 — operational decision flips at $10k from ops-approved
# to finance-cosign).
_CONTRACTUAL_CORRECTION_REASON_TAGS: tuple[str, ...] = (
    "CONTRACT_STALE_PRICE",
    "PROMO_WINDOW_EXPIRED",
    "SAP_MASTER_PRICE_ERROR",
    "CUSTOMER_CONCESSION_LOW",
    "CUSTOMER_CONCESSION_HIGH",
    "BAND_REVIEW_OVERRIDDEN",
    "OTHER",
)

# B2 — CREDIT_BLOCK (CreditHoldReleaseRecipe).
_CREDIT_BLOCK_REASON_TAGS: tuple[str, ...] = (
    "FINANCE_RELEASED_ONE_TIME",
    "CREDIT_LINE_ADJUSTED",
    "CUSTOMER_PREPAY_ARRANGED",
    "RISK_ACCEPTED_OPS",
    "OTHER",
)

# B3 — MANUAL_ORDER_INTAKE (formerly EMAIL_ORDER_ENTRY).
_MANUAL_ORDER_INTAKE_REASON_TAGS: tuple[str, ...] = (
    "EXTRACTION_LOW_CONFIDENCE",
    "FIELD_MISSING_BUYER_CLARIFIED",
    "FIELD_MISSING_BUYER_UNREACHABLE",
    "UNSUPPORTED_FORMAT",
    "MULTI_PO_SPLIT",
    "OTHER",
)

# B4 — EDI_MISMATCH (EdiMismatchRecipe).
# Panel decision (B4.5): NOT aliased with CONTRACTUAL_CORRECTION
# even when sub_type=PRICE_MISMATCH — keeps source-of-record
# distinguishable in audit queries.
_EDI_MISMATCH_REASON_TAGS: tuple[str, ...] = (
    "EDI_PARTNER_MAPPING_ERROR",
    "CUSTOMER_EDI_MISCONFIGURED",
    "SAP_MASTER_DIFF",
    "TOLERANCE_BAND_ACCEPTED",
    "RESUBMITTED_TO_PARTNER",
    "OTHER",
)

# B5 — BACK_ORDER (BackOrderResolutionRecipe). No
# AGENT_MISCLASSIFICATION (recipe outputs are recommendations
# only; reviewer always picks one of the 4 method-specific
# resolutions).
_BACK_ORDER_REASON_TAGS: tuple[str, ...] = (
    "ALTERNATE_WAREHOUSE_APPROVED",
    "SUBSTITUTE_SKU_APPROVED",
    "CUSTOMER_ACCEPTS_DELAY",
    "PARTIAL_FULFILMENT_ACCEPTED",
    "CANCELLATION_ACCEPTED",
    "OTHER",
)

# B6 — PRICE_HOLD_RELEASE (PriceHoldReleaseRecipe).
# `BULK_RELEASE_APPROVED` carries the bulk action ID per
# panel B6.2; each record retains its own tag even when the
# action was a bulk-release (audit slicing per record).
_PRICE_HOLD_RELEASE_REASON_TAGS: tuple[str, ...] = (
    "OPERATIONS_AUTHORISED",
    "FINANCE_AUTHORISED",
    "CONTRACT_VERIFIED",
    "BULK_RELEASE_APPROVED",
    "OTHER",
)

# B7 — OVER_MAX (OverMaxTrimRecipe). Panel split
# OVER_MAX_ALLOWED into customer-request and seasonal-event
# variants (different audit fields per row).
_OVER_MAX_REASON_TAGS: tuple[str, ...] = (
    "TRIM_APPLIED",
    "OVER_MAX_ALLOWED_CUSTOMER_REQUEST",
    "OVER_MAX_ALLOWED_SEASONAL",
    "CUSTOMER_MAX_STALE",
    "OTHER",
)

# B8 — MIN_ORDER_QTY (MoqRoundUpRecipe).
_MIN_ORDER_QTY_REASON_TAGS: tuple[str, ...] = (
    "ROUND_UP_APPLIED",
    "PARTIAL_FULFILMENT_ACCEPTED",
    "CANCELLATION_ACCEPTED",
    "OPT_OUT_VIOLATION_ROUND_UP",
    "OTHER",
)

# B9 — PALLET_CONFIG (PalletAlignmentRecipe). Panel kept the
# four-dimension fan-out (tie / height / freight class /
# case count) since each carries different audit fields
# (carrier-id, freight-delta-borne-by, original-vs-override
# values).
_PALLET_CONFIG_REASON_TAGS: tuple[str, ...] = (
    "TIE_LAYER_OVERRIDDEN",
    "PALLET_HEIGHT_OVERRIDDEN",
    "FREIGHT_CLASS_OVERRIDDEN",
    "CASE_COUNT_OVERRIDDEN",
    "OTHER",
)

# B10 — DELIVERY_DELAY (DeliveryDelayRecipe).
_DELIVERY_DELAY_REASON_TAGS: tuple[str, ...] = (
    "CUSTOMER_ACCEPTS_NEW_DATE",
    "EXPEDITE_APPROVED",
    "CANCELLATION_ACCEPTED",
    "PARTIAL_SPLIT_SHIPMENT",
    "OTHER",
)

# B11 — MASS_PRICING_ERROR. RED-blocking intent (no recipe;
# admin-release path). Panel B11.2: each record carries its
# own tag even when admin-released as a bulk action.
_MASS_PRICING_ERROR_REASON_TAGS: tuple[str, ...] = (
    "BULK_DATA_ERROR_CONFIRMED",
    "BULK_PROMO_EXTENSION",
    "MASS_CONCESSION_HONOURED",
    "FALSE_POSITIVE",
    "OTHER",
)


# Per-intent vocabulary registry (panel 2026-05-10). Every intent
# that has a curated vocabulary lands here; intents without one
# fall back to `_GLOBAL_REASON_TAGS` via the dict comprehension
# below. Once every intent is curated the global fallback is
# read-only (legacy audit-log rows).
_CURATED_INTENT_REASON_TAGS: dict[str, tuple[str, ...]] = {
    "DUPLICATE_PO":            _DUPLICATE_PO_REASON_TAGS,
    "CONTRACTUAL_CORRECTION":  _CONTRACTUAL_CORRECTION_REASON_TAGS,
    "CREDIT_BLOCK":            _CREDIT_BLOCK_REASON_TAGS,
    "MANUAL_ORDER_INTAKE":     _MANUAL_ORDER_INTAKE_REASON_TAGS,
    "EDI_MISMATCH":            _EDI_MISMATCH_REASON_TAGS,
    "BACK_ORDER":              _BACK_ORDER_REASON_TAGS,
    "PRICE_HOLD_RELEASE":      _PRICE_HOLD_RELEASE_REASON_TAGS,
    "OVER_MAX":                _OVER_MAX_REASON_TAGS,
    "MIN_ORDER_QTY":           _MIN_ORDER_QTY_REASON_TAGS,
    "PALLET_CONFIG":           _PALLET_CONFIG_REASON_TAGS,
    "DELIVERY_DELAY":          _DELIVERY_DELAY_REASON_TAGS,
    "MASS_PRICING_ERROR":      _MASS_PRICING_ERROR_REASON_TAGS,
}

INTENT_REASON_TAGS: dict[str, tuple[str, ...]] = {
    intent: _CURATED_INTENT_REASON_TAGS.get(intent, _GLOBAL_REASON_TAGS)
    for intent in AllowedIntent.__args__  # type: ignore[attr-defined]
}


# ---------------------------------------------------------------------------
# Phase 5.3 — grandfather-validator (read-side acceptance of legacy tags)
# ---------------------------------------------------------------------------
#
# After the panel's per-intent curation lands, the
# `_GLOBAL_REASON_TAGS` set is no longer valid for forward
# disposition writes — the validator must enforce per-intent
# narrowing. But existing audit-log rows still carry tags from the
# global set (`customer_concession`, `data_error`, etc.) and the
# hash chain (`policy_audit_log` event-hash) **must not** be
# retroactively invalidated by re-labelling.
#
# Solution: split the validation surface.
#   * `is_valid_reason_tag_for_write(intent, tag)` — forward;
#     enforces the per-intent set strictly.
#   * `is_valid_reason_tag_for_read(tag)` — read-side; accepts
#     the per-intent set OR the legacy global tags.
#
# Routes that accept new dispositions call the WRITE form;
# routes that surface historical audit data call the READ form
# (or skip validation entirely — read-side data already lived
# the lifecycle).


def is_valid_reason_tag_for_write(intent: Optional[str], tag: str) -> bool:
    """Phase 5.2 forward validation.

    Returns True iff `tag` belongs to the per-intent set for
    `intent`. When `intent` is None / unknown / not in the
    curated map (e.g. FAILED-lifecycle records that never
    produced an intent), falls back to `_GLOBAL_REASON_TAGS`.

    The forward-only constraint: a tag valid under the legacy
    `_GLOBAL_REASON_TAGS` set is **not** automatically valid for
    new writes once the per-intent vocabulary lands. Compliance
    audit intent: post-cutover writes use the curated vocabulary;
    new rows that try to use a legacy tag where a curated one
    exists are rejected at the API.

    Read-side acceptance of legacy tags (Phase 5.3 grandfather)
    lives in `is_valid_reason_tag_for_read` — used by audit-trail
    readers + reanalysis paths that surface historical rows.
    """
    if intent is None or intent not in INTENT_REASON_TAGS:
        return tag in _GLOBAL_REASON_TAGS
    return tag in INTENT_REASON_TAGS[intent]


def is_valid_reason_tag_for_read(tag: str) -> bool:
    """Phase 5.3 grandfather-validator.

    Returns True iff `tag` is recognised — either by the curated
    per-intent vocabulary OR the legacy `_GLOBAL_REASON_TAGS` set.
    Used by audit-trail readers + reanalysis paths that surface
    historical disposition rows; never by the disposition write
    path.
    """
    if tag in _GLOBAL_REASON_TAGS:
        return True
    for intent_tags in _CURATED_INTENT_REASON_TAGS.values():
        if tag in intent_tags:
            return True
    return False


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
