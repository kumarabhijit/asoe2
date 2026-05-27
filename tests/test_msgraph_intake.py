"""PARITY-6.1 — Microsoft Graph live email_intake backend.

The live backend wires Microsoft Graph behind the same
``email_intake`` operation contracts the StubGateway already satisfies
(sender_auth, resolve_customer, duplicate_po_pre_check, credit_check,
fetch_message). Routing rules:

  * ``ASOE_EMAIL_INTAKE_DRIVER`` default ``recorded`` → traffic stays
    on the stub (parity with sandbox; reversible by env unset).
  * ``ASOE_EMAIL_INTAKE_DRIVER=graph`` → eligible cases (per
    ``gateways.canary.is_canary_eligible`` on ``case_id``) flow
    through the live backend; the rest stay on the stub.
  * Live-side calls run inside ``gateways.shadow_mode.ShadowRunner`` so
    the diff log accumulates against the Q9 thresholds even at 100%
    canary.
  * Terminal live-side failure (after retries) → routed to
    ``api.dead_letter_queue.record`` so the operator dashboard surfaces
    the orphan, and the stub return is served back to the caller so
    the request still completes.
  * ``fetch_message`` attachment-body egress runs through
    ``gateways.azure_di_egress_redaction.redact_for_azure_di`` before
    leaving our perimeter (Security + ML review requirement).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "fallback")


@pytest.fixture
def _clean_dlq():
    from api import dead_letter_queue
    dead_letter_queue.reset()
    yield
    dead_letter_queue.reset()


@pytest.fixture
def _clean_shadow_log():
    from gateways.shadow_mode import get_diff_log
    get_diff_log().clear()
    yield
    get_diff_log().clear()


def _stub_response(operation: str, data: dict):
    from contracts.models import GatewayResponse
    return GatewayResponse(
        gateway_name="email_intake",
        operation=operation,
        status="SUCCESS",
        data=data,
    )


def _make_stub():
    from gateways.stub import StubGateway
    return StubGateway(
        "email_intake",
        responses={
            "sender_auth": _stub_response(
                "sender_auth",
                {"sender_authorized": True, "auth_method": "domain_match"},
            ),
            "fetch_message": _stub_response(
                "fetch_message",
                {
                    "from_address": "buyer@stub.example",
                    "subject": "stub subject",
                    "body_excerpt": "stub body",
                    "source_email_id": "stub-001",
                },
            ),
        },
    )


class TestDefaultDriverIsRecorded:
    def test_default_driver_routes_to_stub(self, monkeypatch, _clean_dlq):
        """Without an explicit env var, the gateway stays on the stub —
        this is the reversible-by-env contract from the parity plan's
        Rollback section."""
        monkeypatch.delenv("ASOE_EMAIL_INTAKE_DRIVER", raising=False)
        from gateways.msgraph_intake import GraphIntakeGateway
        from contracts.models import GatewayRequest

        stub = _make_stub()
        # Live backend constructor is a sentinel — should NEVER be invoked
        # under the default driver.
        called = {"live": 0}

        class _SentinelLive:
            def __init__(self):
                called["live"] += 1

            def execute(self, request):  # pragma: no cover
                raise AssertionError("live backend should not be called")

        gateway = GraphIntakeGateway(
            stub=stub,
            live_backend_factory=lambda: _SentinelLive(),
        )
        req = GatewayRequest(
            gateway_name="email_intake", operation="sender_auth",
            params={"case_id": "PO-9001"}, trace_id="t-1",
        )
        out = gateway.execute(req)
        assert out.status == "SUCCESS"
        assert out.data["sender_authorized"] is True
        assert called["live"] == 0


class TestCanaryRouting:
    def test_zero_pct_canary_stays_on_stub(
        self, monkeypatch, _clean_dlq, _clean_shadow_log,
    ):
        monkeypatch.setenv("ASOE_EMAIL_INTAKE_DRIVER", "graph")
        monkeypatch.setenv("ASOE_CANARY_PCT_EMAIL_INTAKE", "0.0")
        from gateways.msgraph_intake import GraphIntakeGateway
        from contracts.models import GatewayRequest

        stub = _make_stub()

        class _ShouldNotRun:
            def execute(self, request):  # pragma: no cover
                raise AssertionError("0% canary should not invoke live")

        gateway = GraphIntakeGateway(
            stub=stub, live_backend_factory=lambda: _ShouldNotRun(),
        )
        req = GatewayRequest(
            gateway_name="email_intake", operation="sender_auth",
            params={"case_id": "PO-9001"}, trace_id="t-1",
        )
        out = gateway.execute(req)
        assert out.status == "SUCCESS"

    def test_100_pct_canary_runs_live_and_shadow_diff(
        self, monkeypatch, _clean_dlq, _clean_shadow_log,
    ):
        monkeypatch.setenv("ASOE_EMAIL_INTAKE_DRIVER", "graph")
        monkeypatch.setenv("ASOE_CANARY_PCT_EMAIL_INTAKE", "1.0")
        from gateways.msgraph_intake import GraphIntakeGateway
        from gateways.shadow_mode import get_diff_log
        from contracts.models import GatewayRequest, GatewayResponse

        stub = _make_stub()

        class _Live:
            def execute(self, request):
                # Live returns a slightly different sender — derived
                # field divergence we want recorded.
                return GatewayResponse(
                    gateway_name="email_intake", operation="sender_auth",
                    status="SUCCESS",
                    data={"sender_authorized": True, "auth_method": "spf_pass"},
                )

        gateway = GraphIntakeGateway(
            stub=stub, live_backend_factory=lambda: _Live(),
        )
        req = GatewayRequest(
            gateway_name="email_intake", operation="sender_auth",
            params={"case_id": "PO-9001"}, trace_id="t-1",
        )
        out = gateway.execute(req)
        # Real (live) is authoritative — caller sees live data.
        assert out.data["auth_method"] == "spf_pass"
        # A diff entry was recorded for compliance review.
        entries = get_diff_log()
        assert len(entries) == 1
        assert entries[0].connector == "email_intake"
        # auth_method is classified as derived (see
        # gateways/msgraph_intake.py::_DERIVED_FIELDS). It must land in
        # the derived bucket EXACTLY — pin to one bucket so a future
        # audit-bearing misclassification surfaces here.
        assert "auth_method" in entries[0].derived_diffs
        assert "auth_method" not in entries[0].audit_bearing_diffs


class TestTerminalFailureGoesToDLQ:
    def test_live_failure_after_retries_dlqs_and_falls_back(
        self, monkeypatch, _clean_dlq, _clean_shadow_log,
    ):
        monkeypatch.setenv("ASOE_EMAIL_INTAKE_DRIVER", "graph")
        monkeypatch.setenv("ASOE_CANARY_PCT_EMAIL_INTAKE", "1.0")
        from gateways.msgraph_intake import GraphIntakeGateway
        from contracts.models import GatewayRequest
        from api import dead_letter_queue

        stub = _make_stub()

        class _AlwaysFails:
            def execute(self, request):
                raise RuntimeError("graph 503 — upstream outage")

        gateway = GraphIntakeGateway(
            stub=stub, live_backend_factory=lambda: _AlwaysFails(),
        )
        req = GatewayRequest(
            gateway_name="email_intake", operation="fetch_message",
            params={"case_id": "PO-9001", "tenant_id": "tenant-acme"},
            trace_id="t-1",
        )
        out = gateway.execute(req)
        # Caller sees the stub fallback so the recipe still completes.
        assert out.status == "SUCCESS"
        assert out.data["source_email_id"] == "stub-001"
        # The terminal failure is dead-lettered for the operator.
        entries = dead_letter_queue.list_for_tenant("tenant-acme")
        assert len(entries) == 1
        assert entries[0].source == "graph"
        assert entries[0].operation == "fetch_message"


class TestPiiRedactionOnFetchMessage:
    def test_fetch_message_redacts_attachment_body_on_egress(
        self, monkeypatch, _clean_dlq, _clean_shadow_log,
    ):
        """The fetch_message live path may carry attachment-body bytes
        that AzureDI will later see. The redactor masks SSN / CC / IBAN
        BEFORE the bytes leave our perimeter (Security review)."""
        monkeypatch.setenv("ASOE_EMAIL_INTAKE_DRIVER", "graph")
        monkeypatch.setenv("ASOE_CANARY_PCT_EMAIL_INTAKE", "1.0")
        from gateways.msgraph_intake import GraphIntakeGateway
        from contracts.models import GatewayRequest, GatewayResponse

        stub = _make_stub()

        class _LiveWithPii:
            def execute(self, request):
                return GatewayResponse(
                    gateway_name="email_intake", operation="fetch_message",
                    status="SUCCESS",
                    data={
                        "from_address": "buyer@customer.example",
                        "subject": "PO with sensitive data",
                        "body_excerpt": (
                            "SSN 123-45-6789 and CC 4111111111111111 "
                            "for confirmation."
                        ),
                        "source_email_id": "msg-42",
                    },
                )

        gateway = GraphIntakeGateway(
            stub=stub, live_backend_factory=lambda: _LiveWithPii(),
        )
        req = GatewayRequest(
            gateway_name="email_intake", operation="fetch_message",
            params={"case_id": "PO-9001", "tenant_id": "tenant-acme"},
            trace_id="t-1",
        )
        out = gateway.execute(req)
        assert "123-45-6789" not in out.data["body_excerpt"]
        assert "4111111111111111" not in out.data["body_excerpt"]
        assert "<redacted-ssn>" in out.data["body_excerpt"]
        assert "<redacted-cc>" in out.data["body_excerpt"]


class TestUnknownOperationDelegatesToStub:
    def test_unknown_operation_routes_to_stub(
        self, monkeypatch, _clean_dlq, _clean_shadow_log,
    ):
        """Operations the live backend hasn't implemented yet route to
        the stub unconditionally — additive sub-phase rollout."""
        monkeypatch.setenv("ASOE_EMAIL_INTAKE_DRIVER", "graph")
        monkeypatch.setenv("ASOE_CANARY_PCT_EMAIL_INTAKE", "1.0")
        from gateways.msgraph_intake import GraphIntakeGateway
        from contracts.models import GatewayRequest

        stub = _make_stub()

        class _LiveOnlySupportsSenderAuth:
            SUPPORTED = {"sender_auth"}

            def supports(self, operation):
                return operation in self.SUPPORTED

            def execute(self, request):  # pragma: no cover
                raise AssertionError("should not be called for unsupported op")

        gateway = GraphIntakeGateway(
            stub=stub,
            live_backend_factory=lambda: _LiveOnlySupportsSenderAuth(),
        )
        req = GatewayRequest(
            gateway_name="email_intake", operation="fetch_message",
            params={"case_id": "PO-9001"}, trace_id="t-1",
        )
        out = gateway.execute(req)
        assert out.status == "SUCCESS"
        assert out.data["source_email_id"] == "stub-001"


class TestRegistrationName:
    def test_gateway_registers_under_email_intake_name(self, _clean_dlq):
        from gateways.msgraph_intake import GraphIntakeGateway
        stub = _make_stub()
        gateway = GraphIntakeGateway(
            stub=stub, live_backend_factory=lambda: None,
        )
        # Drop-in for the stub — must preserve the registry name so
        # existing recipe dependency wiring still resolves.
        assert gateway.name == "email_intake"
        assert gateway.health_check() is True
