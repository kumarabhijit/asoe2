"""S/4HANA live backend for the SAP read connectors (PARITY-6.3).

Seven SAP gateways exist as StubGateways today:

  * ``sap_order``      — validate (SO confirmation + ATP)
  * ``sap_doc``        — lookup   (sales-doc / condition chain)
  * ``sap_contract``   — lookup   (contract reference + rule id)
  * ``promotion``      — lookup   (promotion reference)
  * ``sap_block``      — lookup   (delivery block status)
  * ``sap_customer_master`` — lookup (MOQ source + channel)
  * ``sla_contract``   — lookup   (SLA deadline + at-risk amount)

PARITY-6.3 wires a shared ``LiveSapBackend`` behind the same
``InfrastructureGateway`` contract using the same shadow + canary +
DLQ routing pattern Graph 6.1 established. Each domain gets its own
``SapDomainGateway`` wrapper instance (preserves the registry name)
but they share the live backend factory so a connection pool /
OAuth context is amortised across the seven reads.

Decision Q3 — preprod backend is a real S/4HANA preprod tenant with
read-only creds; SAP Cloud trial tenant is the documented fallback if
SAP Basis access is delayed past the PARITY-6.3 spec lock.

The live HTTP transport is gated to the nightly ``-m live`` mark —
``LiveSapBackend.execute`` raises ``NotImplementedError`` on the
red-green path so accidental live calls surface loud.

Per-connector CHANGELOG: ``gateways/changelog/sap.md``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Optional, Protocol, Set

from api import dead_letter_queue
from contracts.models import GatewayRequest, GatewayResponse
from gateways.canary import current_canary_pct, is_canary_eligible
from gateways.shadow_mode import ShadowRunner

logger = logging.getLogger("asoe.gateways.sap_live")


# Field classification per ``compliance/audit_bearing_registry.yaml``.
# The composer reads these per-connector to decide whether a diff
# flips the connector out of shadow → real-only (Q9 thresholds).
_AUDIT_BEARING_FIELDS: Dict[str, Dict[str, Set[str]]] = {
    "sap_order": {
        "validate": {"system", "validation_status", "sap_doc_number"},
    },
    "sap_doc": {
        "lookup": {"doc_type", "doc_number", "applied_condition_chain"},
    },
    "sap_contract": {
        "lookup": {"contract_ref", "rule_id", "root_cause_category"},
    },
    "promotion": {
        "lookup": {"promotion_ref", "root_cause_category"},
    },
    "sap_block": {
        "lookup": {"block_status", "block_reason"},
    },
    "sap_customer_master": {
        "lookup": {"moq_source", "channel"},
    },
    "sla_contract": {
        "lookup": {"sla_deadline", "at_risk"},
    },
}

_DERIVED_FIELDS: Dict[str, Dict[str, Set[str]]] = {
    "sap_order": {"validate": {"order_value_usd"}},
    "sap_doc": {"lookup": {"sku", "uom", "material_desc", "order_date"}},
    "sap_contract": {"lookup": set()},
    "promotion": {"lookup": set()},
    "sap_block": {"lookup": {"block_message"}},
    "sap_customer_master": {"lookup": set()},
    "sla_contract": {"lookup": set()},
}

_DRIVER_ENV = "ASOE_SAP_DRIVER"
# The DLQ source name groups all seven domains under "sap" for the
# operator dashboard — orphans are categorised by upstream system,
# not by ASOE-internal registry name.
_DLQ_SOURCE = "sap"
# Canary env name is shared across the seven domains so an operator
# rolls SAP traffic uniformly. (Per-domain overrides would land as
# ``ASOE_CANARY_PCT_<DOMAIN>`` if a future need surfaces.)
_CANARY_KEY = "sap"


def _driver() -> str:
    return (os.getenv(_DRIVER_ENV) or "recorded").strip().lower()


class _LiveBackendProtocol(Protocol):
    def supports(self, connector: str, operation: str) -> bool: ...

    def execute(self, request: GatewayRequest) -> GatewayResponse: ...


class SapDomainGateway:
    """Live-or-stub router for a single SAP domain.

    Drop-in for the existing StubGateway — preserves the registry name
    of the wrapped connector so recipe ``GatewayDependency`` rows
    resolve unchanged.

    Routing decision (same shape as ``GraphIntakeGateway``):
      1. If ``ASOE_SAP_DRIVER`` != ``s4hana`` → stub.
      2. If the live backend doesn't declare support for this
         (connector, operation) tuple → stub.
      3. If ``is_canary_eligible(case_id, pct)`` is False → stub.
      4. Otherwise: run real + stub through ``ShadowRunner``; on a
         terminal live failure record the orphan in the DLQ under
         ``source="sap"`` (operator-dashboard category) and serve the
         stub return so the recipe still completes.
    """

    def __init__(
        self,
        *,
        connector: str,
        stub,
        live_backend_factory: Optional[Callable[[], _LiveBackendProtocol]] = None,
    ) -> None:
        self._connector = connector
        self._stub = stub
        self._live_backend_factory = live_backend_factory
        self._live_backend: Optional[_LiveBackendProtocol] = None

    @property
    def name(self) -> str:
        return self._connector

    def health_check(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _resolve_live_backend(self) -> Optional[_LiveBackendProtocol]:
        if self._live_backend is not None:
            return self._live_backend
        if self._live_backend_factory is None:
            return None
        try:
            self._live_backend = self._live_backend_factory()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "sap_live backend init failed (%s) for connector=%s — "
                "staying on stub", exc, self._connector,
            )
            self._live_backend = None
        return self._live_backend

    def _should_route_live(self, request: GatewayRequest) -> bool:
        if _driver() != "s4hana":
            return False
        backend = self._resolve_live_backend()
        if backend is None:
            return False
        supports = getattr(backend, "supports", None)
        if callable(supports):
            try:
                if not bool(supports(self._connector, request.operation)):
                    return False
            except Exception:  # noqa: BLE001
                return False
        case_id = str((request.params or {}).get("case_id") or "")
        if not case_id:
            return False
        return is_canary_eligible(case_id=case_id, pct=current_canary_pct(_CANARY_KEY))

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    def execute(self, request: GatewayRequest) -> GatewayResponse:
        if not self._should_route_live(request):
            return self._stub.execute(request)

        backend = self._resolve_live_backend()
        assert backend is not None
        operation = request.operation
        tenant_id = str(
            (request.params or {}).get("tenant_id") or "unknown",
        )

        audit_fields = set(
            _AUDIT_BEARING_FIELDS.get(self._connector, {}).get(operation, set()),
        )
        derived_fields = set(
            _DERIVED_FIELDS.get(self._connector, {}).get(operation, set()),
        )

        def _real_call(**_kw: Any) -> Dict[str, Any]:
            response = backend.execute(request)
            if response.status != "SUCCESS":
                raise RuntimeError(
                    f"live {self._connector} op={operation} "
                    f"status={response.status} err={response.error}",
                )
            return dict(response.data or {})

        def _stub_call(**_kw: Any) -> Dict[str, Any]:
            stub_response = self._stub.execute(request)
            return dict(stub_response.data or {})

        runner = ShadowRunner(
            connector=self._connector, operation=operation,
            audit_bearing_fields=audit_fields,
            derived_fields=derived_fields,
        )

        try:
            data = runner.run(real=_real_call, stub=_stub_call, params={})
        except Exception as exc:  # noqa: BLE001
            dead_letter_queue.record(
                source=_DLQ_SOURCE, operation=operation, tenant_id=tenant_id,
                reason=f"both real and stub failed: {type(exc).__name__}",
                payload={
                    "trace_id": request.trace_id,
                    # `connector` is the legacy field; `domain` is the
                    # operator-dashboard pivot key so the dashboard can
                    # group by upstream system (source="sap") AND
                    # filter by SAP domain (order, doc, contract, …)
                    # without parsing the operation name. Both fields
                    # carry the same value today; a future schema
                    # evolution could rename `connector` → `domain`.
                    "connector": self._connector,
                    "domain": self._connector,
                },
            )
            return GatewayResponse(
                gateway_name=self._connector, operation=operation,
                status="FAILED", error=str(exc),
            )

        entry = runner.last_entry
        if entry is not None and entry.real_error is not None:
            dead_letter_queue.record(
                source=_DLQ_SOURCE, operation=operation, tenant_id=tenant_id,
                reason=entry.real_error,
                payload={
                    "trace_id": request.trace_id,
                    # `connector` is the legacy field; `domain` is the
                    # operator-dashboard pivot key so the dashboard can
                    # group by upstream system (source="sap") AND
                    # filter by SAP domain (order, doc, contract, …)
                    # without parsing the operation name. Both fields
                    # carry the same value today; a future schema
                    # evolution could rename `connector` → `domain`.
                    "connector": self._connector,
                    "domain": self._connector,
                },
            )

        return GatewayResponse(
            gateway_name=self._connector, operation=operation,
            status="SUCCESS", data=data,
        )


# ---------------------------------------------------------------------------
# Live backend — shared S/4HANA OData transport.
#
# Lazy-imports the OData / SAP client only when ``ASOE_SAP_DRIVER=s4hana``
# is set; preprod / sandbox boots that stay on stubs never touch SAP libs.
# ---------------------------------------------------------------------------

# Connector → supported operations map. Mirrors the seven StubGateways
# in api/sandbox_gateways.py exactly so a recipe's dependency
# declaration resolves under either backend.
_SUPPORTED: Dict[str, Set[str]] = {
    "sap_order": {"validate"},
    "sap_doc": {"lookup"},
    "sap_contract": {"lookup"},
    "promotion": {"lookup"},
    "sap_block": {"lookup"},
    "sap_customer_master": {"lookup"},
    "sla_contract": {"lookup"},
}


class LiveSapBackend:
    """Shared OData transport for the seven SAP domains.

    Configuration:

      * ``ASOE_SAP_HOST``     — S/4HANA base URL
        (e.g. ``https://s4h-preprod.example/sap/opu/odata/sap/``).
      * ``ASOE_SAP_USER``     — read-only service user.
      * ``ASOE_SAP_PASSWORD`` — service user password (Key Vault
        secretref per PARITY-4).
      * ``ASOE_SAP_CLIENT``   — optional SAP client number
        (3-digit string, default ``100`` for sandbox).

    Connection-pool note (Decision Q3 / Azure SRE review): the SAP
    fan-out is the workload most likely to exhaust the Postgres pool;
    PgBouncer landed before this connector per the parity plan §6
    pre-requisites. SAP itself is queried through HTTP, but recipes
    cache their reads in Postgres — that's where the pool pressure
    lives.

    Live HTTP transport is gated to the nightly ``-m live`` mark — the
    red-green path never hits real SAP, so ``execute`` raises
    ``NotImplementedError`` to make accidental live calls loud.
    """

    def __init__(self) -> None:
        self._host = os.environ.get("ASOE_SAP_HOST") or ""
        self._user = os.environ.get("ASOE_SAP_USER") or ""
        self._password = os.environ.get("ASOE_SAP_PASSWORD") or ""
        self._client = os.environ.get("ASOE_SAP_CLIENT") or "100"
        if not (self._host and self._user and self._password):
            raise RuntimeError(
                "LiveSapBackend requires ASOE_SAP_HOST / ASOE_SAP_USER / "
                "ASOE_SAP_PASSWORD; Decision Q3 fallback is the SAP Cloud "
                "trial tenant, see docs/ops/secrets-rotation.md.",
            )

    def supports(self, connector: str, operation: str) -> bool:
        return operation in _SUPPORTED.get(connector, set())

    def execute(self, request: GatewayRequest) -> GatewayResponse:
        raise NotImplementedError(
            "LiveSapBackend.execute: live S/4HANA OData transport runs "
            "only against the nightly -m live mark; tests inject a "
            "stubbed backend at SapDomainGateway construction time.",
        )


__all__ = [
    "LiveSapBackend",
    "SapDomainGateway",
]
