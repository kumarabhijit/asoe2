from __future__ import annotations

from contracts.policy import CREDIT_AUTHORIZED_ROLES, CREDIT_EXPOSURE_TOLERANCE


def release_credit_hold(order_id: str, requester_role: str, credit_limit: float, current_exposure: float):
    if requester_role not in CREDIT_AUTHORIZED_ROLES:
        return {"status": "BLOCKED", "reason": "Insufficient permissions for Credit Release."}
    if (current_exposure - credit_limit) > CREDIT_EXPOSURE_TOLERANCE:
        return {
            "status": "REJECTED",
            "reason": "Exposure exceeds limit by >$5,000. Manual Finance review required.",
        }
    return {"status": "RELEASED", "order_id": order_id, "workflow": "AUTO_APPROVED"}
