from __future__ import annotations

# Pallet Alignment Recipe
#
# Deterministic detection + suggestion for pallet-configuration
# violations: broken layers (SD-PLT-001) and partial pallets
# (SD-PLT-002). Produces a per-line suggested plan that aligns orders
# to full layers / full pallets where possible.
#
# Inputs:
#   order_id                     : str          — host ERP order id
#   lines                        : List[Dict]   — {sku, description,
#                                                 layer_qty, pallet_qty,
#                                                 ordered_qty, uom}
#   min_fill_pct                 : float        — partial-pallet
#                                                 threshold
#                                                 (SD-PLT-002)
#   broken_layer_fill_pct        : float        — any fill ratio above
#                                                 which a line is
#                                                 considered broken
#                                                 (SD-PLT-001)
#
# Outputs (Dict[str, Any]):
#   status                : "COMPLETE" | "REVIEW_REQUIRED" | "FAILED"
#   classification        : "NO_VIOLATION" | "BROKEN_LAYER" |
#                           "PARTIAL_PALLET" | "MIXED_VIOLATION"
#   total_ordered_cases   : float
#   loose_cases_total     : float   — cases not on a full pallet
#   order_line_count      : int
#   lines                 : List[Dict] — per-line detail incl. fill%
#                                         and violation_type
#   suggested_plan        : List[Dict] — {sku, current, suggested,
#                                          delta, layers, full_pallets,
#                                          reason}
#   recommended_action    : "ROUND_DOWN_TO_LAYERS" | "ACCEPT_AS_IS" |
#                           "REVIEW"

from typing import Any, Dict, List


def align_pallets(
    order_id: str,
    lines: List[Dict[str, Any]] | None,
    min_fill_pct: float,
    broken_layer_fill_pct: float,
) -> Dict[str, Any]:
    lines = lines or []
    if not lines:
        return {
            "status": "FAILED",
            "classification": None,
            "total_ordered_cases": 0.0,
            "loose_cases_total": 0.0,
            "order_line_count": 0,
            "lines": [],
            "suggested_plan": [],
            "recommended_action": None,
            "order_id": order_id,
            "reason": "lines must be a non-empty list.",
        }

    enriched_lines: List[Dict[str, Any]] = []
    suggested_plan: List[Dict[str, Any]] = []
    total_ordered = 0.0
    total_loose = 0.0
    has_broken = False
    has_partial = False

    for line in lines:
        sku = line.get("sku", "")
        description = line.get("description")
        layer_qty = float(line.get("layer_qty") or 0.0)
        pallet_qty = float(line.get("pallet_qty") or 0.0)
        ordered = float(line.get("ordered_qty") or 0.0)
        uom = line.get("uom") or "CS"
        total_ordered += ordered

        if pallet_qty <= 0 or layer_qty <= 0:
            # Can't reason about alignment without a valid pallet/layer.
            enriched_lines.append({
                "sku": sku,
                "description": description,
                "uom": uom,
                "layer_qty": layer_qty,
                "pallet_qty": pallet_qty,
                "ordered_qty": ordered,
                "complete_layers": 0,
                "loose_qty": ordered,
                "full_pallets": 0,
                "pallet_fill_pct": 0.0,
                "violation_type": "Invalid Pallet Spec",
            })
            total_loose += ordered
            suggested_plan.append({
                "sku": sku,
                "description": description,
                "current": ordered,
                "suggested": ordered,
                "delta": 0.0,
                "layers": 0,
                "full_pallets": 0,
                "reason": "Missing / zero layer or pallet qty — cannot align.",
            })
            continue

        full_pallets = int(ordered // pallet_qty)
        residual_after_pallets = ordered - (full_pallets * pallet_qty)
        complete_layers = int(residual_after_pallets // layer_qty)
        loose = residual_after_pallets - (complete_layers * layer_qty)
        pallet_fill_pct = (
            1.0 if residual_after_pallets == 0
            else residual_after_pallets / pallet_qty
        )
        total_loose += loose

        if loose > 0:
            violation = "Broken Layer"
            has_broken = True
            target_qty = full_pallets * pallet_qty + complete_layers * layer_qty
        elif residual_after_pallets > 0 and pallet_fill_pct < min_fill_pct:
            violation = "Partial Pallet"
            has_partial = True
            # Round down to full pallets for partial.
            target_qty = full_pallets * pallet_qty
        elif residual_after_pallets > 0 and pallet_fill_pct < broken_layer_fill_pct:
            # Between min_fill and broken_layer_fill — acceptable
            # partial; keep as-is.
            violation = None
            target_qty = ordered
        else:
            violation = None
            target_qty = ordered

        enriched_lines.append({
            "sku": sku,
            "description": description,
            "uom": uom,
            "layer_qty": layer_qty,
            "pallet_qty": pallet_qty,
            "ordered_qty": ordered,
            "complete_layers": complete_layers,
            "loose_qty": round(loose, 3),
            "full_pallets": full_pallets,
            "pallet_fill_pct": round(pallet_fill_pct, 4),
            "violation_type": violation,
        })

        suggested_plan.append({
            "sku": sku,
            "description": description,
            "current": ordered,
            "suggested": round(target_qty, 3),
            "delta": round(target_qty - ordered, 3),
            "layers": complete_layers,
            "full_pallets": full_pallets,
            "reason": _reason_for(violation, layer_qty, pallet_qty, complete_layers),
        })

    if has_broken and has_partial:
        classification = "MIXED_VIOLATION"
    elif has_broken:
        classification = "BROKEN_LAYER"
    elif has_partial:
        classification = "PARTIAL_PALLET"
    else:
        classification = "NO_VIOLATION"

    recommended_action = (
        "ROUND_DOWN_TO_LAYERS"
        if has_broken or has_partial
        else "ACCEPT_AS_IS"
    )
    status = (
        "REVIEW_REQUIRED"
        if has_broken or has_partial
        else "COMPLETE"
    )

    return {
        "status": status,
        "classification": classification,
        "total_ordered_cases": round(total_ordered, 3),
        "loose_cases_total": round(total_loose, 3),
        "order_line_count": len(enriched_lines),
        "lines": enriched_lines,
        "suggested_plan": suggested_plan,
        "recommended_action": recommended_action,
        "order_id": order_id,
    }


def _reason_for(
    violation: str | None,
    layer_qty: float,
    pallet_qty: float,
    complete_layers: int,
) -> str:
    if violation == "Broken Layer":
        return (
            f"Round down to {complete_layers} full layer(s) of "
            f"{int(layer_qty)} {pallet_qty and 'CS' or ''} — preserves layer "
            f"integrity (SD-PLT-001)."
        )
    if violation == "Partial Pallet":
        return (
            f"Round down to full pallets of {int(pallet_qty)} to eliminate "
            f"partial-pallet freight waste (SD-PLT-002)."
        )
    return "Line aligned to pallet config; no alignment change needed."
