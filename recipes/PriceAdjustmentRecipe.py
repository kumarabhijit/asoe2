from __future__ import annotations

from typing import Any, Dict


def execute_price_correction(order_id: str, line_item: int, requested_price: float, erp_context: Dict[str, Any]):
    """Apply a price correction for a contractual mismatch.

    Thresholds are injected via *erp_context* by the orchestration layer —
    this recipe never imports from ``contracts.policy`` directly so the
    same logic can serve different customer / vendor threshold sets.

    Required erp_context keys:
        base_price            — SAP base price for the line item
        max_discount_allowed  — maximum discount fraction (e.g. 0.15 for 15 %)
        condition_type        — SAP condition type to apply (e.g. "YK07")
    """
    base_price = erp_context["base_price"]
    threshold = erp_context["max_discount_allowed"]
    condition_type = erp_context["condition_type"]

    calculated_discount = (base_price - requested_price) / base_price
    if calculated_discount > threshold:
        return {
            "status": "FAILED",
            "reason": f"Discount {calculated_discount:.2%} exceeds {threshold:.0%} threshold.",
        }

    payload = {
        "OrderID": order_id,
        "Item": line_item,
        "ConditionType": condition_type,
        "NewValue": requested_price,
        "ReasonCode": "CUST_MATCH",
    }
    return {
        "status": "SUCCESS",
        "applied_condition": condition_type,
        "new_net_price": requested_price,
        "payload": payload,
    }
