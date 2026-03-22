from __future__ import annotations

from typing import Sequence


def release_credit_hold(
    order_id: str,
    requester_role: str,
    credit_limit: float,
    current_exposure: float,
    authorized_roles: Sequence[str] = (),
    exposure_tolerance: float = 0.0,
):
    """Evaluate and release a credit hold.

    Thresholds are injected by the orchestration layer — this recipe never
    imports from ``contracts.policy`` directly so the same logic can serve
    different customer / vendor threshold sets.

    Args:
        authorized_roles:    Roles permitted to auto-release (e.g. from policy).
        exposure_tolerance:  Maximum (exposure − limit) delta before requiring
                             manual Finance review.
    """
    if requester_role not in authorized_roles:
        return {"status": "BLOCKED", "reason": "Insufficient permissions for Credit Release."}
    if (current_exposure - credit_limit) > exposure_tolerance:
        return {
            "status": "REJECTED",
            "reason": (
                f"Exposure exceeds limit by >${exposure_tolerance:,.0f}. "
                "Manual Finance review required."
            ),
        }
    return {"status": "RELEASED", "order_id": order_id, "workflow": "AUTO_APPROVED"}
