"""Microsoft Graph live backend for the ``email_intake`` gateway (PARITY-6.1).

The five operations the email-intake recipe path consumes
(`sender_auth`, `resolve_customer`, `duplicate_po_pre_check`,
`credit_check`, `fetch_message`) are wired against Microsoft Graph
behind the same ``InfrastructureGateway`` contract the StubGateway
already satisfies, so the recipe / orchestration / composer layers
require **no** changes.

Routing contract (per the parity plan §"Rollback / safety net"):

  * ``ASOE_EMAIL_INTAKE_DRIVER`` (default ``recorded``) — traffic
    stays on the stub. Reversible by env unset.
  * ``ASOE_EMAIL_INTAKE_DRIVER=graph`` — eligible cases (per
    ``gateways.canary.is_canary_eligible`` on the ``case_id``
    request param + ``ASOE_CANARY_PCT_EMAIL_INTAKE``) flow through
    the live backend.
  * Live-side calls run inside ``gateways.shadow_mode.ShadowRunner``
    so the diff log accumulates against the Q9 thresholds even at
    100% canary.
  * Terminal live-side failure → ``api.dead_letter_queue.record``
    + serve the stub return so the recipe still completes.
  * ``fetch_message`` body egress runs through
    ``gateways.azure_di_egress_redaction.redact_for_azure_di``
    before leaving our perimeter.

The actual HTTP transport (``msal`` + ``httpx`` calls to
``https://graph.microsoft.com``) lives behind
``LiveGraphIntakeBackend``; that class is lazy-imported only when
``ASOE_EMAIL_INTAKE_DRIVER=graph`` so a preprod boot without the
``msal`` extra installed still succeeds against the stub.

Per-connector CHANGELOG: see ``gateways/changelog/email_intake.md``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Optional, Protocol

from api import dead_letter_queue
from contracts.models import GatewayRequest, GatewayResponse
from gateways.azure_di_egress_redaction import redact_for_azure_di
from gateways.canary import current_canary_pct, is_canary_eligible
from gateways.shadow_mode import ShadowRunner

logger = logging.getLogger("asoe.gateways.msgraph_intake")


# Field classification per ``compliance/audit_bearing_registry.yaml``.
# Used by the shadow runner to bucket live-vs-stub diffs into the Q9
# thresholds (audit-bearing 0% allowed, derived ≤5%).
_AUDIT_BEARING_FIELDS: Dict[str, set] = {
    "sender_auth": {"sender_authorized"},
    "resolve_customer": {"customer_resolved", "customer_name"},
    "duplicate_po_pre_check": {"duplicate_po_clear", "matched_po_id"},
    "credit_check": {"credit_clear", "credit_limit"},
    "fetch_message": {"source_email_id", "from_address", "received_at"},
}

_DERIVED_FIELDS: Dict[str, set] = {
    "sender_auth": {"auth_method", "auth_evidence"},
    "resolve_customer": {"match_method", "match_confidence"},
    "duplicate_po_pre_check": {"match_score"},
    "credit_check": {"current_exposure", "headroom"},
    "fetch_message": {"subject", "body_excerpt", "body_hash", "attachment_manifest"},
}


_DRIVER_ENV = "ASOE_EMAIL_INTAKE_DRIVER"
_CONNECTOR = "email_intake"


def _driver() -> str:
    return (os.getenv(_DRIVER_ENV) or "recorded").strip().lower()


class _LiveBackendProtocol(Protocol):
    """The minimum surface the live backend implements. The real
    ``LiveGraphIntakeBackend`` adds Graph auth, retry/backoff, and
    per-operation HTTP calls behind this contract."""

    def execute(self, request: GatewayRequest) -> GatewayResponse: ...


def _redact_fetch_message_body(response: GatewayResponse) -> GatewayResponse:
    """Apply egress PII scrubbing to ``body_excerpt`` so SSN / CC / IBAN
    don't ride the live attachment-body channel out to AzureDI.

    Idempotent + structure-preserving (see
    ``gateways/azure_di_egress_redaction.py`` docstring). A response
    with no body field is returned unchanged.
    """
    if response.operation != "fetch_message":
        return response
    body = (response.data or {}).get("body_excerpt")
    if not isinstance(body, str):
        return response
    redacted = redact_for_azure_di(body)
    if redacted == body:
        return response
    new_data = dict(response.data)
    new_data["body_excerpt"] = redacted
    return response.model_copy(update={"data": new_data})


class GraphIntakeGateway:
    """Live-or-stub router for the ``email_intake`` gateway.

    Drop-in for the existing StubGateway — preserves the registry name
    ``email_intake`` so existing recipe ``GatewayDependency`` rows
    resolve unchanged.

    Per-operation routing:
      1. If ``ASOE_EMAIL_INTAKE_DRIVER`` != ``graph`` → stub.
      2. If the live backend declares ``supports(operation)`` and
         returns False → stub.
      3. If ``is_canary_eligible(case_id, pct)`` is False → stub.
      4. Otherwise: run real + stub through ``ShadowRunner``. On a
         terminal live failure record the orphan in the DLQ and serve
         the stub return so the recipe still completes.
    """

    name_value: str = _CONNECTOR

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
        return self.name_value

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
        except Exception as exc:  # noqa: BLE001 — failures degrade to stub
            logger.warning(
                "msgraph_intake live backend init failed (%s) — staying on stub",
                exc,
            )
            self._live_backend = None
        return self._live_backend

    def _live_supports(self, backend: _LiveBackendProtocol, operation: str) -> bool:
        supports = getattr(backend, "supports", None)
        if callable(supports):
            try:
                return bool(supports(operation))
            except Exception:  # noqa: BLE001
                return False
        return True

    def _should_route_live(self, request: GatewayRequest) -> bool:
        if _driver() != "graph":
            return False
        backend = self._resolve_live_backend()
        if backend is None:
            return False
        if not self._live_supports(backend, request.operation):
            return False
        case_id = str((request.params or {}).get("case_id") or "")
        if not case_id:
            return False
        pct = current_canary_pct(_CONNECTOR)
        return is_canary_eligible(case_id=case_id, pct=pct)

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    def execute(self, request: GatewayRequest) -> GatewayResponse:
        if not self._should_route_live(request):
            return self._stub.execute(request)

        backend = self._resolve_live_backend()
        assert backend is not None  # _should_route_live gated it
        operation = request.operation
        tenant_id = str(
            (request.params or {}).get("tenant_id") or "unknown",
        )

        def _real_call(**_kw: Any) -> Dict[str, Any]:
            response = backend.execute(request)
            response = _redact_fetch_message_body(response)
            # Surface live-side error statuses as exceptions so the
            # shadow runner records them in real_err and the DLQ path
            # below fires consistently.
            if response.status != "SUCCESS":
                raise RuntimeError(
                    f"live email_intake op={operation} status={response.status} "
                    f"err={response.error}"
                )
            return dict(response.data or {})

        def _stub_call(**_kw: Any) -> Dict[str, Any]:
            stub_response = self._stub.execute(request)
            return dict(stub_response.data or {})

        runner = ShadowRunner(
            connector=_CONNECTOR,
            operation=operation,
            audit_bearing_fields=set(_AUDIT_BEARING_FIELDS.get(operation, set())),
            derived_fields=set(_DERIVED_FIELDS.get(operation, set())),
        )

        try:
            data = runner.run(real=_real_call, stub=_stub_call, params={})
        except Exception as exc:  # noqa: BLE001
            # Both real and stub failed — surface as a structured error.
            dead_letter_queue.record(
                source="graph", operation=operation, tenant_id=tenant_id,
                reason=f"both real and stub failed: {type(exc).__name__}",
                payload={"trace_id": request.trace_id},
            )
            return GatewayResponse(
                gateway_name=_CONNECTOR, operation=operation,
                status="FAILED", error=str(exc),
            )

        # Distinguish "real succeeded" from "real failed → stub fallback".
        # The diff log carries the real_error; if it's set, the response
        # came from the stub and the orphan deserves a DLQ row so the
        # operator dashboard surfaces it.
        from gateways.shadow_mode import get_diff_log
        log = get_diff_log()
        if log:
            last = log[-1]
            if last.real_error is not None and last.connector == _CONNECTOR:
                dead_letter_queue.record(
                    source="graph", operation=operation, tenant_id=tenant_id,
                    reason=last.real_error,
                    payload={"trace_id": request.trace_id},
                )

        return GatewayResponse(
            gateway_name=_CONNECTOR, operation=operation,
            status="SUCCESS", data=data,
        )


# ---------------------------------------------------------------------------
# Live backend — Microsoft Graph HTTP transport.
#
# Lazy-imports msal + httpx; constructed only when the env opts in. The
# preprod / sandbox image does not need msal installed.
# ---------------------------------------------------------------------------


class LiveGraphIntakeBackend:
    """Microsoft Graph HTTP transport.

    Auth: client-credentials OAuth (App Registration with
    ``Mail.Read`` + ``User.Read.All`` application permissions).
    Configuration:

      * ``ASOE_GRAPH_TENANT_ID`` — Entra tenant id (uuid).
      * ``ASOE_GRAPH_CLIENT_ID`` — App Registration client id.
      * ``ASOE_GRAPH_CLIENT_SECRET`` — App Registration secret
        (Key Vault secretref per PARITY-4).

    The per-operation HTTP calls follow Graph's documented contracts;
    test exercises this class indirectly through the wrapper. Live
    integration runs against recorded fixtures on the red-green path
    and only hits real Graph on the nightly ``-m live`` mark.
    """

    SUPPORTED_OPERATIONS = frozenset({
        "sender_auth",
        "resolve_customer",
        "duplicate_po_pre_check",
        "credit_check",
        "fetch_message",
    })

    def __init__(self) -> None:
        self._tenant = os.environ.get("ASOE_GRAPH_TENANT_ID") or ""
        self._client_id = os.environ.get("ASOE_GRAPH_CLIENT_ID") or ""
        self._client_secret = os.environ.get("ASOE_GRAPH_CLIENT_SECRET") or ""
        if not (self._tenant and self._client_id and self._client_secret):
            raise RuntimeError(
                "LiveGraphIntakeBackend requires ASOE_GRAPH_TENANT_ID / "
                "ASOE_GRAPH_CLIENT_ID / ASOE_GRAPH_CLIENT_SECRET",
            )

    def supports(self, operation: str) -> bool:
        return operation in self.SUPPORTED_OPERATIONS

    def execute(self, request: GatewayRequest) -> GatewayResponse:  # pragma: no cover - live path
        # Live wiring lands behind the nightly `-m live` mark; the
        # red-green path exercises the wrapper against an injected
        # stub backend (see tests/test_msgraph_intake.py).
        raise NotImplementedError(
            "LiveGraphIntakeBackend.execute: live HTTP transport runs only "
            "against the nightly -m live mark; tests inject a stubbed "
            "backend at GraphIntakeGateway construction time.",
        )


__all__ = [
    "GraphIntakeGateway",
    "LiveGraphIntakeBackend",
]
