from __future__ import annotations

from typing import Any, Dict

from contracts.policy import MAX_DISCOUNT_ALLOWED, PRICE_CONDITION_TYPE


def execute_price_correction(order_id: str, line_item: int, requested_price: float, erp_context: Dict[str, Any]):
    base_price = erp_context.get("base_price")
    threshold = erp_context.get("max_discount_allowed", MAX_DISCOUNT_ALLOWED)
    calculated_discount = (base_price - requested_price) / base_price
    if calculated_discount > threshold:
        return {
            "status": "FAILED",
            "reason": f"Discount {calculated_discount:.2%} exceeds {MAX_DISCOUNT_ALLOWED:.0%} threshold.",
        }

    payload = {
        "OrderID": order_id,
        "Item": line_item,
        "ConditionType": PRICE_CONDITION_TYPE,
        "NewValue": requested_price,
        "ReasonCode": "CUST_MATCH",
    }
    return {
        "status": "SUCCESS",
        "applied_condition": "YK07",
        "new_net_price": requested_price,
        "payload": payload,
    }
