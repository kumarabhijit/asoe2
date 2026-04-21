from __future__ import annotations

# Delivery Delay Resolution Recipe
#
# Deterministic classification + ranked alternate-delivery options for
# a shipment whose ETA has slipped past the planned delivery date.
#
# Inputs:
#   order_id             : str          — host ERP order id
#   planned_date         : str          — ISO-8601 planned delivery
#   projected_eta        : str          — ISO-8601 current best ETA
#   minor_days           : int          — SD-DELAY-001 lower bound
#   severe_days          : int          — SD-DELAY-002 threshold
#   carrier              : Optional[str]
#   route                : Optional[str]
#   alternate_options    : Optional[List[Dict]] — caller-supplied
#                                                 expedite / split /
#                                                 reschedule candidates
#                                                 (each with type, title,
#                                                 description, new_eta,
#                                                 extra_cost)
#
# Outputs (Dict[str, Any]):
#   status                : "COMPLETE" | "REVIEW_REQUIRED" | "ESCALATE" | "FAILED"
#   classification        : "ON_TIME" | "MINOR_DELAY" | "SEVERE_DELAY"
#   days_late             : int         — projected_eta − planned_date
#   delay_category        : str         — caller-supplied or "UNSPECIFIED"
#   primary_option        : Dict | None — top-ranked alternate
#   alternate_options     : List[Dict]  — ranked, lowest cost first
#                                         within each equivalence class
#   recommended_action    : "EXPEDITE" | "SPLIT_SHIP" | "RESCHEDULE" |
#                           "NO_ACTION"

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def resolve_delivery_delay(
    order_id: str,
    planned_date: str,
    projected_eta: str,
    minor_days: int,
    severe_days: int,
    carrier: Optional[str] = None,
    route: Optional[str] = None,
    delay_category: Optional[str] = None,
    alternate_options: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    try:
        planned_dt = _parse_iso(planned_date)
        eta_dt = _parse_iso(projected_eta)
    except ValueError as exc:
        return {
            "status": "FAILED",
            "classification": None,
            "days_late": None,
            "delay_category": None,
            "primary_option": None,
            "alternate_options": [],
            "recommended_action": None,
            "order_id": order_id,
            "reason": f"Invalid ISO-8601 date input: {exc}",
        }

    delta_days = (eta_dt - planned_dt).days

    if delta_days < minor_days:
        return {
            "status": "COMPLETE",
            "classification": "ON_TIME",
            "days_late": max(0, delta_days),
            "delay_category": delay_category or "NONE",
            "primary_option": None,
            "alternate_options": [],
            "recommended_action": "NO_ACTION",
            "order_id": order_id,
            "carrier": carrier,
            "route": route,
        }

    classification = (
        "SEVERE_DELAY" if delta_days >= severe_days else "MINOR_DELAY"
    )

    ranked = _rank_options(
        classification=classification,
        alternate_options=alternate_options or [],
    )
    primary = ranked[0] if ranked else None
    recommended_action = _recommended_action(classification, primary)
    status = "ESCALATE" if classification == "SEVERE_DELAY" else "REVIEW_REQUIRED"

    return {
        "status": status,
        "classification": classification,
        "days_late": delta_days,
        "delay_category": delay_category or "UNSPECIFIED",
        "primary_option": primary,
        "alternate_options": ranked,
        "recommended_action": recommended_action,
        "order_id": order_id,
        "carrier": carrier,
        "route": route,
    }


def _parse_iso(value: str) -> datetime:
    """Parse ISO-8601; tolerate both naive and aware strings."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _rank_options(
    classification: str,
    alternate_options: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Rank options.

    MINOR_DELAY: prefer lowest extra_cost, keep type grouping
    stable (EXPEDITE before SPLIT_SHIP before RESCHEDULE).

    SEVERE_DELAY: prefer RESCHEDULE/SPLIT first (buyer coordination
    over expensive expedite), then fall back to EXPEDITE if the
    caller flagged one as recommended.
    """
    if not alternate_options:
        return []

    # Preserve caller-supplied "recommended: true" as a hard pin to
    # position 0.
    pinned = [o for o in alternate_options if o.get("recommended")]
    others = [o for o in alternate_options if not o.get("recommended")]

    type_weight_minor = {"EXPEDITE": 0, "SPLIT_SHIP": 1, "RESCHEDULE": 2}
    type_weight_severe = {"RESCHEDULE": 0, "SPLIT_SHIP": 1, "EXPEDITE": 2}
    weights = (
        type_weight_severe
        if classification == "SEVERE_DELAY"
        else type_weight_minor
    )

    others.sort(
        key=lambda o: (
            weights.get(o.get("type", ""), 99),
            float(o.get("extra_cost") or 0.0),
        )
    )
    return pinned + others


def _recommended_action(
    classification: str,
    primary: Optional[Dict[str, Any]],
) -> str:
    if primary is None:
        return "RESCHEDULE" if classification == "SEVERE_DELAY" else "EXPEDITE"
    return primary.get("type", "RESCHEDULE")
