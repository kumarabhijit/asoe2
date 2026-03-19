from __future__ import annotations


def release_credit_hold(order_id: str, requester_role: str, credit_limit: float, current_exposure: float):
    authorized_roles = ["ORDER_MANAGER", "FINANCE_DIRECTOR"]
    if requester_role not in authorized_roles:
        return {"status": "BLOCKED", "reason": "Insufficient permissions for Credit Release."}
    if (current_exposure - credit_limit) > 5000:
        return {
            "status": "REJECTED",
            "reason": "Exposure exceeds limit by >$5,000. Manual Finance review required.",
        }
    return {"status": "RELEASED", "order_id": order_id, "workflow": "AUTO_APPROVED"}
