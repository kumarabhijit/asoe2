"""OMS live backend (PARITY-6.4).

Follows the GraphIntakeGateway 6.1 + SapDomainGateway 6.3 routing
pattern with one OMS-specific addition: a
``record_post_success_orphan`` helper for the post-recipe-success
failure window. Every other connector is read-only, so the orphan
risk doesn't exist; OMS writes (order acceptance, order cancellation,
ATP commitment) can succeed at the recipe-level audit then fail
downstream, leaving the audit log saying 'order accepted' while OMS
never recorded the write. That orphan MUST surface to the operator
dashboard for reconciliation.

Env contract:
  * ``ASOE_OMS_DRIVER`` default ``recorded`` → stub.
  * ``ASOE_OMS_DRIVER=live`` + canary-eligible case_id → live.
  * Live transport raises ``NotImplementedError`` until the nightly
    ``-m live`` mark wires the HTTP body.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Optional, Protocol

from api import dead_letter_queue
from contracts.models import GatewayRequest, GatewayResponse
from gateways.canary import current_canary_pct, is_canary_eligible
from gateways.shadow_mode import ShadowRunner

logger = logging.getLogger("asoe.gateways.oms_live")


_AUDIT_BEARING_FIELDS: Dict[str, set] = {
    "get_fulfillment_status": {"fulfilled"},
    "get_inventory_snapshot": {"primary_dc", "atp_date"},
    "get_matched_po_details": {
        "has_revision_indicator", "line_items_identical",
        "cancellation_target", "customer_id",
    },
    "get_price_hold_status": {"status"},
    # OMS write operations — audit-bearing IDs the auditor uses to
    # correlate the in-system order with the OMS write.
    "write_order_acceptance": {"oms_order_id"},
    "write_order_cancellation": {"oms_order_id", "cancelled_at"},
}

_DERIVED_FIELDS: Dict[str, set] = {
    "get_fulfillment_status": set(),
    "get_inventory_snapshot": {
        "alternate_warehouses", "substitutes", "production", "inbound_po",
    },
    "get_matched_po_details": {
        "days_between", "detection_method", "matching_fields",
        "differing_fields", "original_order", "duplicate_order",
    },
    "get_price_hold_status": set(),
    "write_order_acceptance": set(),
    "write_order_cancellation": set(),
}


_DRIVER_ENV = "ASOE_OMS_DRIVER"
_CONNECTOR = "oms"
_DLQ_SOURCE = "oms"


def _driver() -> str:
    return (os.getenv(_DRIVER_ENV) or "recorded").strip().lower()


class _LiveBackendProtocol(Protocol):
    def execute(self, request: GatewayRequest) -> GatewayResponse: ...


# ---------------------------------------------------------------------------
# Post-recipe-success orphan helper
# ---------------------------------------------------------------------------

def record_post_success_orphan(
    *,
    tenant_id: str,
    operation: str,
    recipe_run_id: str,
    reason: str,
) -> None:
    """Record an OMS post-recipe-success failure in the DLQ.

    Use case (the contract from the PARITY-6.4 prompt): a recipe
    completes GREEN, the audit log records "order accepted", but the
    subsequent OMS write fails (502 after retry exhausted, OMS
    rejection due to inventory race, …). The downstream system never
    recorded the write but the audit trail says it did. The operator
    dashboard surfaces these orphans for reconciliation; the auditor
    correlates by ``recipe_run_id``.

    Distinct from the in-line ShadowRunner failure path: that one
    fires INSIDE the gateway call. This one fires when the gateway
    call already returned SUCCESS but a separate effect (the OMS
    write triggered by the recipe's resolution) failed later.

    Raises ``ValueError`` on empty tenant_id — every orphan MUST be
    attributable, and an empty tenant id is a bug.
    """
    if not tenant_id:
        raise ValueError(
            "record_post_success_orphan: tenant_id is mandatory; the "
            "DLQ row must be tenant-scoped for the operator dashboard",
        )
    dead_letter_queue.record(
        source=_DLQ_SOURCE,
        operation=operation,
        tenant_id=tenant_id,
        reason=f"post-recipe-success OMS write failed: {reason}",
        payload={"recipe_run_id": recipe_run_id},
    )


class OmsGateway:
    """Live-or-stub router for the OMS connector. Mirrors
    GraphIntakeGateway 6.1 / SapDomainGateway 6.3 routing."""

    def __init__(
        self,
        *,
        stub,
        live_backend_factory: Optional[Callable[[], _LiveBackendProtocol]] = None,
    ) -> None:
        self._stub = stub
        self._live_backend_factory = live_backend_factory
        self._live_backend: Optional[_LiveBackendProtocol] = None

    @property
    def name(self) -> str:
        return _CONNECTOR

    def health_check(self) -> bool:
        return True

    def _resolve_live_backend(self) -> Optional[_LiveBackendProtocol]:
        if self._live_backend is not None:
            return self._live_backend
        if self._live_backend_factory is None:
            return None
        try:
            self._live_backend = self._live_backend_factory()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "oms_live backend init failed (%s) — staying on stub", exc,
            )
            self._live_backend = None
        return self._live_backend

    def _should_route_live(self, request: GatewayRequest) -> bool:
        if _driver() != "live":
            return False
        backend = self._resolve_live_backend()
        if backend is None:
            return False
        supports = getattr(backend, "supports", None)
        if callable(supports):
            try:
                if not bool(supports(request.operation)):
                    return False
            except Exception:  # noqa: BLE001
                return False
        case_id = str((request.params or {}).get("case_id") or "")
        if not case_id:
            return False
        return is_canary_eligible(
            case_id=case_id, pct=current_canary_pct(_CONNECTOR),
        )

    def execute(self, request: GatewayRequest) -> GatewayResponse:
        if not self._should_route_live(request):
            return self._stub.execute(request)

        backend = self._resolve_live_backend()
        assert backend is not None
        operation = request.operation
        tenant_id = str(
            (request.params or {}).get("tenant_id") or "unknown",
        )

        def _real_call(**_kw: Any) -> Dict[str, Any]:
            response = backend.execute(request)
            if response.status != "SUCCESS":
                raise RuntimeError(
                    f"live oms op={operation} status={response.status} "
                    f"err={response.error}",
                )
            return dict(response.data or {})

        def _stub_call(**_kw: Any) -> Dict[str, Any]:
            stub_response = self._stub.execute(request)
            return dict(stub_response.data or {})

        runner = ShadowRunner(
            connector=_CONNECTOR, operation=operation,
            audit_bearing_fields=set(_AUDIT_BEARING_FIELDS.get(operation, set())),
            derived_fields=set(_DERIVED_FIELDS.get(operation, set())),
        )

        try:
            data = runner.run(real=_real_call, stub=_stub_call, params={})
        except Exception as exc:  # noqa: BLE001
            dead_letter_queue.record(
                source=_DLQ_SOURCE, operation=operation, tenant_id=tenant_id,
                reason=f"both real and stub failed: {type(exc).__name__}",
                payload={"trace_id": request.trace_id},
            )
            return GatewayResponse(
                gateway_name=_CONNECTOR, operation=operation,
                status="FAILED", error=str(exc),
            )

        entry = runner.last_entry
        if entry is not None and entry.real_error is not None:
            dead_letter_queue.record(
                source=_DLQ_SOURCE, operation=operation, tenant_id=tenant_id,
                reason=entry.real_error,
                payload={"trace_id": request.trace_id},
            )

        return GatewayResponse(
            gateway_name=_CONNECTOR, operation=operation,
            status="SUCCESS", data=data,
        )


# ---------------------------------------------------------------------------
# Live OMS backend.
# ---------------------------------------------------------------------------

class LiveOmsBackend:
    """OMS HTTP transport.

    Configuration:
      * ``ASOE_OMS_BASE_URL`` — OMS API base.
      * ``ASOE_OMS_API_KEY``  — API key (Key Vault secretref).
      * ``ASOE_OMS_TIMEOUT_S`` — optional per-call timeout override
        (default from ``contracts.policy.GATEWAY_TIMEOUT_S['oms']``).

    Live HTTP transport is gated to the nightly ``-m live`` mark — the
    red-green path uses the OMS StubGateway. ``execute`` raises
    ``NotImplementedError`` so an accidental live call surfaces loud.
    """

    SUPPORTED_OPERATIONS = frozenset({
        "get_fulfillment_status",
        "get_inventory_snapshot",
        "get_matched_po_details",
        "get_price_hold_status",
        "write_order_acceptance",
        "write_order_cancellation",
    })

    def __init__(self) -> None:
        self._base = (os.environ.get("ASOE_OMS_BASE_URL") or "").strip()
        self._key = (os.environ.get("ASOE_OMS_API_KEY") or "").strip()
        if not (self._base and self._key):
            raise RuntimeError(
                "LiveOmsBackend requires ASOE_OMS_BASE_URL + ASOE_OMS_API_KEY",
            )

    def supports(self, operation: str) -> bool:
        return operation in self.SUPPORTED_OPERATIONS

    def execute(self, request: GatewayRequest) -> GatewayResponse:
        raise NotImplementedError(
            "LiveOmsBackend.execute: live HTTP transport runs only against "
            "the nightly -m live mark; tests inject a stubbed backend at "
            "OmsGateway construction time.",
        )


__all__ = [
    "LiveOmsBackend",
    "OmsGateway",
    "record_post_success_orphan",
]
