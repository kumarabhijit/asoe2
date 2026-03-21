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
