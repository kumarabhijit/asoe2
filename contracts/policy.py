from __future__ import annotations

# Policy configuration — externalized business thresholds
#
# Centralises every tunable threshold that was previously hardcoded in
# recipes and orchestration utilities.  The values may later be loaded
# from a config file, environment, or policy service; for now they are
# module-level constants that serve as the single source of truth.
#
# CLAUDE.md §1: recipes execute, skills guide; thresholds live here.

# ---------------------------------------------------------------------------
# Price Adjustment thresholds
# ---------------------------------------------------------------------------

MAX_DISCOUNT_ALLOWED: float = 0.15
"""Maximum discount (as a decimal fraction) before a price correction is rejected."""

PRICE_CONDITION_TYPE: str = "YK07"
"""SAP condition type applied for customer-match price corrections."""

# ---------------------------------------------------------------------------
# Credit Hold Release thresholds
# ---------------------------------------------------------------------------

CREDIT_AUTHORIZED_ROLES: tuple[str, ...] = ("ORDER_MANAGER", "FINANCE_DIRECTOR")
"""Roles permitted to auto-release a credit hold."""

CREDIT_EXPOSURE_TOLERANCE: float = 5_000.0
"""Maximum (exposure − limit) delta before requiring manual Finance review."""

# ---------------------------------------------------------------------------
# Duplicate PO Detection thresholds
# ---------------------------------------------------------------------------

DUPLICATE_PO_THRESHOLD_AUTO_BLOCK: float = 0.90
"""Composite score at or above which a PO is auto-blocked."""

DUPLICATE_PO_THRESHOLD_REVIEW_REQUIRED: float = 0.70
"""Composite score at or above which a PO requires manual review."""

DUPLICATE_PO_THRESHOLD_SOFT_FLAG: float = 0.50
"""Composite score at or above which a PO is soft-flagged."""

# Autonomy level per resolution action (spec §3.3).
# L4 = full autonomy (auto-execute), L3 = act & inform, L2 = recommend
# (human must approve), L1 = observe only.
DUPLICATE_PO_AUTONOMY_LEVELS: dict[str, str] = {
    "BLOCK_AND_NOTIFY": "L3",
    "MERGE": "L2",
    "SUPERSEDE": "L2",
    "ALLOW_BOTH": "L3",
    "ESCALATE": "L1",
    "REQUEST_BUYER_CONFIRMATION": "L2",
}
"""Maps resolution action → autonomy level for routing decisions."""

# ---------------------------------------------------------------------------
# Mass-update / line-count threshold
# ---------------------------------------------------------------------------

MASS_UPDATE_LINE_COUNT_THRESHOLD: int = 10
"""Line count above which an event is classified as a mass update risk."""

# ---------------------------------------------------------------------------
# Circuit breaker thresholds (orchestration/utils.py)
# ---------------------------------------------------------------------------

CIRCUIT_BREAKER_MAX_UPDATES: int = 50
"""Maximum pricing updates allowed per 5-minute window."""

CIRCUIT_BREAKER_MAX_VARIANCE: float = 10_000.0
"""Maximum total dollar variance per batch before halting."""

# ---------------------------------------------------------------------------
# Re-analysis (human-triggered graph replay) thresholds
# ---------------------------------------------------------------------------

REANALYSIS_MAX_ATTEMPTS: int = 3
"""Maximum number of human-initiated re-analyses per exception.

Bounded to prevent outcome-shopping — repeatedly re-running the graph in
hopes of a different Compliance Shadow verdict. The prior outcome is always
preserved in the exception's reanalysis_history for audit.
"""

# ---------------------------------------------------------------------------
# Discrepancy threshold
# ---------------------------------------------------------------------------

DISCREPANCY_THRESHOLD: float = 0.15
"""Maximum price discrepancy (%) before flagging as outside threshold."""

# ---------------------------------------------------------------------------
# Four-eyes high-value override (Phase 2 #5)
# ---------------------------------------------------------------------------

HIGH_VALUE_OVERRIDE_THRESHOLD_USD: float = 10_000.0
"""Financial impact at/above which a manager override requires a second
reviewer to cosign before the action is applied.

When the exception's financial_impact_usd meets or exceeds this threshold,
POST /exceptions/{id}/override transitions the record to PENDING_COSIGN
instead of RESOLVED; a different manager+ must then POST to
/exceptions/{id}/override/cosign to approve (applies the action) or reject
(restores prior lifecycle). Standard SOX control under §404 — any single
manager cannot unilaterally authorize a material change."""

# ---------------------------------------------------------------------------
# Price Hold Release thresholds
# ---------------------------------------------------------------------------

PRICE_HOLD_TOLERANCE_PCT: float = 0.02
"""Absolute variance (decimal fraction) at or below which a held order may
be auto-released without review. Variance is |po_price − sap_base_price| /
sap_base_price."""

PRICE_HOLD_HARD_BLOCK_PCT: float = 0.10
"""Absolute variance above which a held order is hard-blocked (REJECTED)
rather than escalated to manual review. Between tolerance and hard-block,
the order is escalated (MANUAL_REVIEW_REQUIRED)."""

# ---------------------------------------------------------------------------
# EDI Mismatch autonomy levels (per sub_type)
# ---------------------------------------------------------------------------

EDI_MISMATCH_AUTONOMY_LEVELS: dict[str, str] = {
    "SKU_MISMATCH": "L3",
    "QTY_MISMATCH": "L2",
    "UOM_MISMATCH": "L2",
    "SHIP_TO_MISMATCH": "L1",
}
"""Maps EDI 850 line-mismatch sub_type → autonomy level. L1 = observe only,
L2 = recommend (human must approve), L3 = act & inform. PRICE_MISMATCH is
absent by design — those events are routed to CONTRACTUAL_CORRECTION /
PriceAdjustmentRecipe.py at classifier time and never reach this recipe."""

# ---------------------------------------------------------------------------
# Back-Order (OOS) thresholds
# Per prototype spec SD-OOS-001 (<50% gap) and SD-OOS-002 (>=50% gap).
# ---------------------------------------------------------------------------

BACK_ORDER_SEVERE_GAP_PCT: float = 0.50
"""Gap percentage at or above which a back-order is classified SEVERE.
SD-OOS-002 rule. Below this, the recipe can recommend split-shipment or
alternate-DC fulfilment; at/above, escalation to a buyer decision is
mandatory."""

# ---------------------------------------------------------------------------
# Over-Max quantity thresholds
# Per prototype spec SD-OM-001 (exceeds contract) and SD-OM-002 (>50%).
# ---------------------------------------------------------------------------

OVER_MAX_SEVERE_EXCEEDANCE_PCT: float = 0.50
"""Exceedance percentage above contract max at/above which an order is
SEVERE. SD-OM-002 rule. Below this, automated trim-to-max is permitted;
at/above, sales-manager review is required before any trim action."""

# ---------------------------------------------------------------------------
# Minimum Order Quantity thresholds
# Per prototype spec SD-MOQ-001 (<25% shortfall) and SD-MOQ-002 (>=25%).
# ---------------------------------------------------------------------------

MOQ_SEVERE_SHORTFALL_PCT: float = 0.25
"""Shortfall percentage at/above which a MOQ shortfall is SEVERE.
SD-MOQ-002 rule. Below this, automated round-up to MOQ is permitted;
at/above, the order needs a KNMT waiver or sales-manager escalation."""

MOQ_UPLIFT_REVIEW_PCT: float = 0.10
"""Round-up uplift at/above which operator sign-off is required even if
the underlying shortfall is below SEVERE. Prevents silent large-value
upsizes from slipping through the automated path."""

# ---------------------------------------------------------------------------
# Pallet configuration thresholds
# Per prototype specs SD-PLT-001 (broken layer) and SD-PLT-002 (partial).
# ---------------------------------------------------------------------------

PALLET_CONFIG_MIN_FILL_PCT: float = 0.90
"""Pallet fill percentage below which the recipe flags a partial-pallet
violation (SD-PLT-002). Used by PalletAlignmentRecipe to decide whether
to suggest round-down to full layers or accept the partial pallet."""

PALLET_CONFIG_BROKEN_LAYER_FILL_PCT: float = 1.00
"""Fill percentage above which a row is considered to have a broken
layer overage (SD-PLT-001 — ordered qty spans partial layer(s)). Only
round-down alignment is safe above this ratio."""

# ---------------------------------------------------------------------------
# Delivery delay thresholds
# Per prototype specs SD-DELAY-001 (2-4 days) and SD-DELAY-002 (>=5 days).
# ---------------------------------------------------------------------------

DELIVERY_DELAY_MINOR_DAYS: int = 2
"""Minimum days-late threshold for a MINOR delay (SD-DELAY-001).
Below this the recipe short-circuits with no action needed."""

DELIVERY_DELAY_SEVERE_DAYS: int = 5
"""Days-late threshold at/above which a delivery delay is classified
SEVERE (SD-DELAY-002). Below this the recipe can recommend expedite /
split-ship; at/above, reschedule-to-later-window plus buyer notification
is required."""
