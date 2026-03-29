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
# Discrepancy threshold
# ---------------------------------------------------------------------------

DISCREPANCY_THRESHOLD: float = 0.15
"""Maximum price discrepancy (%) before flagging as outside threshold."""
