from __future__ import annotations

# Submit-to-ERP Recipe (ADR-042 Phase 3 — the order-entry ERP write).
#
# Deterministic construction of the SAP sales-order-create (BAPI / EDI 850)
# payload from an operator-reviewed extracted order. This is the financially
# binding leg of the Customer-Inbox order-entry flow: it runs only after a
# human authorises the submit via a disposition (ADR-042 §2.2.6), Compliance
# Shadow clears, and four-eyes cosign clears for materiality >$10k (the
# threshold is computed from the SAP re-price — ADR-042 DoR #3).
#
# Guardrail #1 (skills guide; recipes execute): all submit logic lives here as
# a pure function. No I/O, no LLM calls, no gateway calls — the orchestration
# layer applies the ERP write as a declared GatewayEffect after this recipe
# returns. Operator corrections are applied here over the extracted values with
# a before/after audit so the immutable disposition audit trail records exactly
# what the human changed (ADR-042 §2.2.6 / ADR-023 hash-chained audit).
#
# Inputs:
#   order_id      : str               — host trace / order identifier
#   customer_bp   : Optional[str]     — resolved sold-to business partner
#   header        : Dict[str, Any]    — extracted OrderEntryHeader fields
#   line_items    : List[Dict]        — extracted OrderEntryLineItem dicts
#   corrections   : Optional[Dict]    — operator edits:
#                                         {"header": {field: value},
#                                          "lines": {line_num: {field: value}}}
#
# Output (Dict[str, Any]):
#   status               : "SUCCESS" | "REJECTED"
#   reason               : Optional[str]  — populated when REJECTED
#   erp_payload          : Dict           — the sales-order-create payload
#   corrections_applied  : List[Dict]     — before/after audit of each edit
#   line_count           : int
#   total_value_usd      : float          — sum(qty * unit_price), deterministic
#
# Invariants:
#   - Pure function: no side effects.
#   - REJECTED (a valid terminal — Guardrail #5) when the order can't be
#     submitted: no line items, or any line missing material / non-positive qty.
#   - Corrections to a line that isn't present are ignored (never fabricate a
#     line); header corrections to unknown fields are ignored.

from typing import Any, Dict, List, Optional

# Header fields the operator may correct + that flow into the ERP payload.
_HEADER_FIELDS: tuple[str, ...] = (
    "customer_po", "order_type", "sales_org", "dist_channel",
    "ship_window_from", "ship_window_to", "requested_date",
)

# Per-line fields the operator may correct.
_LINE_FIELDS: tuple[str, ...] = ("material", "quantity", "uom", "unit_price")


def build_erp_submission(
    *,
    order_id: str,
    customer_bp: Optional[str],
    header: Dict[str, Any],
    line_items: List[Dict[str, Any]],
    corrections: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the SAP sales-order-create payload from the reviewed order.

    See the module docstring for the input/output contract. Applies operator
    corrections over the extracted values with a before/after audit, then
    validates submittability.
    """
    corrections = corrections or {}
    corrections_applied: List[Dict[str, Any]] = []

    # --- Apply header corrections -------------------------------------------
    eff_header: Dict[str, Any] = dict(header or {})
    for field, after in (corrections.get("header") or {}).items():
        if field not in _HEADER_FIELDS:
            continue  # ignore unknown header fields — never invent contract keys
        before = eff_header.get(field)
        if before != after:
            eff_header[field] = after
            corrections_applied.append(
                {"field": field, "before": before, "after": after}
            )

    # --- Apply per-line corrections -----------------------------------------
    line_edits: Dict[str, Dict[str, Any]] = corrections.get("lines") or {}
    eff_lines: List[Dict[str, Any]] = []
    for raw in line_items or []:
        line = dict(raw)
        line_num = str(line.get("line_num", ""))
        edits = line_edits.get(line_num) or {}
        for field, after in edits.items():
            if field not in _LINE_FIELDS:
                continue
            before = line.get(field)
            if before != after:
                line[field] = after
                corrections_applied.append(
                    {"line_num": line_num, "field": field,
                     "before": before, "after": after}
                )
        eff_lines.append(line)

    # --- Validate submittability (explicit failure is correct) --------------
    reject_reason = _reject_reason(eff_lines)
    if reject_reason is not None:
        return {
            "status": "REJECTED",
            "reason": reject_reason,
            "erp_payload": {},
            "corrections_applied": corrections_applied,
            "line_count": len(eff_lines),
            "total_value_usd": 0.0,
        }

    payload_lines = [
        {
            "line_num": str(li.get("line_num", "")),
            "material": li.get("material"),
            "quantity": _as_float(li.get("quantity")),
            "uom": li.get("uom"),
            "unit_price": _as_float(li.get("unit_price")),
        }
        for li in eff_lines
    ]
    total = round(
        sum((pl["quantity"] or 0.0) * (pl["unit_price"] or 0.0) for pl in payload_lines),
        2,
    )

    erp_payload = {
        "order_id": order_id,
        "doc_type": eff_header.get("order_type"),
        "sales_org": eff_header.get("sales_org"),
        "dist_channel": eff_header.get("dist_channel"),
        "customer_bp": customer_bp,
        "customer_po": eff_header.get("customer_po"),
        "requested_date": eff_header.get("requested_date"),
        "line_items": payload_lines,
    }

    return {
        "status": "SUCCESS",
        "reason": None,
        "erp_payload": erp_payload,
        "corrections_applied": corrections_applied,
        "line_count": len(payload_lines),
        "total_value_usd": total,
    }


def _reject_reason(lines: List[Dict[str, Any]]) -> Optional[str]:
    if not lines:
        return "No line items to submit."
    for li in lines:
        if not li.get("material"):
            return f"Line {li.get('line_num')!r} is missing a material."
        qty = _as_float(li.get("quantity"))
        if qty is None or qty <= 0:
            return f"Line {li.get('line_num')!r} has a non-positive quantity."
    return None


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
