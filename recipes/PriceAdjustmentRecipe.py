from __future__ import annotations

from typing import Any, Dict


def execute_price_correction(order_id: str, line_item: int, requested_price: float, erp_context: Dict[str, Any]):
    base_price = erp_context.get("base_price")
    threshold = erp_context.get("max_discount_allowed", 0.15)
    calculated_discount = (base_price - requested_price) / base_price
    if calculated_discount > threshold:
        return {
            "status": "FAILED",
            "reason": f"Discount {calculated_discount:.2%} exceeds 15% threshold.",
        }

    payload = {
        "OrderID": order_id,
        "Item": line_item,
        "ConditionType": "YK07",
        "NewValue": requested_price,
        "ReasonCode": "CUST_MATCH",
    }
    return {
        "status": "SUCCESS",
        "applied_condition": "YK07",
        "new_net_price": requested_price,
        "payload": payload,
    }
