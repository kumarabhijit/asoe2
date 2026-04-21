from __future__ import annotations

# Over-Max Quantity Trim Recipe
#
# Deterministic classification + per-line trim-plan generation when a
# PO's total quantity exceeds the contract-max-qty ceiling.
#
# Inputs:
#   order_id                  : str              — host ERP order id
#   total_ordered             : float            — sum across all lines
#   max_qty                   : float            — contract ceiling
#   severe_exceedance_pct     : float            — SD-OM-002 threshold
#   order_lines               : List[Dict]       — {sku, description,
#                                                  qty, max_line_qty,
#                                                  is_even_layer_item}
#   unit_cost_per_line        : Dict[str, float] — sku → cost (for
#                                                  at_risk compute)
#
# Outputs (Dict[str, Any]):
#   status             : "COMPLETE" | "REVIEW_REQUIRED" | "ESCALATE" | "FAILED"
#   classification     : "NO_EXCEEDANCE" | "MINOR" | "SEVERE"
#   excess_qty         : float
#   exceedance_pct     : float
#   at_risk            : float
#   trim_plan          : List[Dict]  — {sku, ordered, trimmed_to,
#                                       delta, action: "TRIM" | "SKIP" |
#                                       "OK"}
#   recommended_action : "TRIM_TO_MAX" | "ESCALATE" | "NO_ACTION"
#
# Invariants:
#   - total_ordered > 0, max_qty > 0 (validated upstream).
#   - Even-layer lines are preferred for TRIM action over broken-layer
#     lines so pallet integrity is preserved where possible.

from typing import Any, Dict, List


def trim_over_max(
    order_id: str,
    total_ordered: float,
    max_qty: float,
    severe_exceedance_pct: float,
    order_lines: List[Dict[str, Any]] | None = None,
    unit_cost_per_line: Dict[str, float] | None = None,
) -> Dict[str, Any]:
    if total_ordered <= 0 or max_qty <= 0:
        return {
            "status": "FAILED",
            "classification": None,
            "excess_qty": None,
            "exceedance_pct": None,
            "at_risk": None,
            "trim_plan": [],
            "recommended_action": None,
            "order_id": order_id,
            "reason": (
                f"total_ordered and max_qty must be > 0; "
                f"got {total_ordered}, {max_qty}."
            ),
        }

    excess = max(0.0, total_ordered - max_qty)
    exceedance_pct = excess / max_qty if max_qty > 0 else 0.0

    if excess == 0:
        return {
            "status": "COMPLETE",
            "classification": "NO_EXCEEDANCE",
            "excess_qty": 0.0,
            "exceedance_pct": 0.0,
            "at_risk": 0.0,
            "trim_plan": [],
            "recommended_action": "NO_ACTION",
            "order_id": order_id,
        }

    classification = (
        "SEVERE" if exceedance_pct > severe_exceedance_pct else "MINOR"
    )

    trim_plan = _plan_trim(
        excess=excess,
        order_lines=order_lines or [],
        unit_cost_per_line=unit_cost_per_line or {},
    )
    at_risk = round(
        sum(
            abs(float(row.get("delta") or 0.0))
            * float((unit_cost_per_line or {}).get(row["sku"], 0.0))
            for row in trim_plan
            if row["action"] == "TRIM"
        ),
        2,
    )

    status = "ESCALATE" if classification == "SEVERE" else "REVIEW_REQUIRED"
    recommended_action = (
        "ESCALATE" if classification == "SEVERE" else "TRIM_TO_MAX"
    )

    return {
        "status": status,
        "classification": classification,
        "excess_qty": round(excess, 3),
        "exceedance_pct": round(exceedance_pct, 4),
        "at_risk": at_risk,
        "trim_plan": trim_plan,
        "recommended_action": recommended_action,
        "order_id": order_id,
    }


def _plan_trim(
    excess: float,
    order_lines: List[Dict[str, Any]],
    unit_cost_per_line: Dict[str, float],
) -> List[Dict[str, Any]]:
    """Allocate the excess to trim across lines.

    Strategy:
      1. Each line over its own max_line_qty is trimmed to its max
         (action=TRIM).
      2. If residual excess remains, trim even-layer lines
         proportionally (action=TRIM), preserving pallet integrity.
      3. Broken-layer (non-even) lines get action=SKIP — caller must
         escalate those to a human.
      4. Lines at or below their max get action=OK.
    """
    plan: List[Dict[str, Any]] = []
    remaining_excess = excess

    # Phase 1: per-line ceiling trims.
    for line in order_lines:
        sku = line.get("sku", "")
        ordered = float(line.get("qty") or 0.0)
        line_max = float(line.get("max_line_qty") or ordered)
        is_even = bool(line.get("is_even_layer_item", True))
        if ordered > line_max:
            delta = round(line_max - ordered, 3)
            remaining_excess = max(0.0, remaining_excess + delta)
            plan.append({
                "sku": sku,
                "description": line.get("description"),
                "ordered": ordered,
                "trimmed_to": line_max,
                "delta": delta,
                "action": "TRIM",
                "reason": "Line exceeds max_line_qty.",
                "is_even_layer_item": is_even,
            })
        else:
            plan.append({
                "sku": sku,
                "description": line.get("description"),
                "ordered": ordered,
                "trimmed_to": ordered,
                "delta": 0.0,
                "action": "OK",
                "reason": "Line within contract max.",
                "is_even_layer_item": is_even,
            })

    # Phase 2: allocate residual excess to even-layer lines only.
    if remaining_excess > 0:
        eligible_ok_rows = [
            row for row in plan
            if row["action"] == "OK" and row["is_even_layer_item"]
        ]
        total_ok_qty = sum(row["ordered"] for row in eligible_ok_rows)
        if total_ok_qty > 0:
            for row in eligible_ok_rows:
                share = row["ordered"] / total_ok_qty
                trim_by = round(share * remaining_excess, 3)
                if trim_by <= 0:
                    continue
                row["trimmed_to"] = round(row["ordered"] - trim_by, 3)
                row["delta"] = -trim_by
                row["action"] = "TRIM"
                row["reason"] = (
                    f"Proportional trim ({share:.1%}) to hit contract max."
                )

        # Remaining broken-layer rows: flag SKIP.
        for row in plan:
            if row["action"] == "OK" and not row["is_even_layer_item"]:
                # Leave OK if no trim still needed; otherwise SKIP.
                # (Pure classification — caller escalates SKIP rows.)
                continue

    # Mark anything that still needs trimming but can't be (broken
    # layer with no even-layer coverage) as SKIP.
    for row in plan:
        if row["action"] == "OK" and not row["is_even_layer_item"] and excess > 0:
            # Only flag broken-layer rows as SKIP if we couldn't allocate
            # the full excess to even-layer lines.
            unresolved = remaining_excess - sum(
                abs(r["delta"]) for r in plan if r["action"] == "TRIM"
            )
            if unresolved > 0.001:
                row["action"] = "SKIP"
                row["reason"] = "Broken-layer line — requires human review."

    return plan
