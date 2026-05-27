"""PARITY-6.3 — SAP S/4HANA live backend wrapper.

Seven SAP read connectors (sap_order, sap_doc, sap_contract,
promotion, sap_block, sap_customer_master, sla_contract) each have a
StubGateway today. PARITY-6.3 wires a shared ``LiveSapBackend`` behind
the same routing pattern Graph 6.1 established:

  * ``ASOE_SAP_DRIVER`` default ``recorded`` → traffic stays on the
    seven stubs. Reversible by env unset.
  * ``ASOE_SAP_DRIVER=s4hana`` + canary-eligible case_id → live
    backend via ShadowRunner; real is authoritative, stub diff'd
    against Q9 thresholds.
  * Terminal live failure → ``api.dead_letter_queue.record`` +
    stub fallback so the recipe still completes (no SAP write path —
    every domain is read-only — but the orphan still surfaces to the
    operator dashboard).
  * No PII egress concern on SAP reads (the data is going INBOUND).

Per Decision Q3 the preferred backend is a real S/4HANA preprod
tenant with read-only credentials; the SAP Cloud trial is the
documented fallback. Either way, the live HTTP transport is gated to
the nightly ``-m live`` mark — the red-green path uses the recorded
stubs, so ``LiveSapBackend.execute`` raises ``NotImplementedError``
to make accidental live calls loud.
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
def _clean_shadow():
    from gateways.shadow_mode import get_diff_log
    get_diff_log().clear()
    yield
    get_diff_log().clear()


def _stub_for(connector: str):
    from gateways.stub import StubGateway
    from contracts.models import GatewayResponse
    # Single-op stub seeded with a known shape; the production stubs
    # mirror api/sandbox_gateways.py.
    op = "validate" if connector == "sap_order" else "lookup"
    return StubGateway(
        connector,
        responses={
            op: GatewayResponse(
                gateway_name=connector, operation=op,
                status="SUCCESS",
                data={"system": "stub", "stub_field": "stub-value"},
            ),
        },
    )


# ------------------------------------------------------------------
# Domain-by-domain routing
# ------------------------------------------------------------------

SAP_DOMAINS = (
    "sap_order",
    "sap_doc",
    "sap_contract",
    "promotion",
    "sap_block",
    "sap_customer_master",
    "sla_contract",
)


class TestDefaultDriverIsRecorded:
    def test_default_routes_to_stub(self, monkeypatch, _clean_dlq):
        monkeypatch.delenv("ASOE_SAP_DRIVER", raising=False)
        from gateways.sap_live import SapDomainGateway
        from contracts.models import GatewayRequest

        stub = _stub_for("sap_order")
        called = {"live": 0}

        class _Sentinel:
            def __init__(self):
                called["live"] += 1

            def execute(self, request):  # pragma: no cover
                raise AssertionError("live backend should not be called")

        gw = SapDomainGateway(
            connector="sap_order",
            stub=stub,
            live_backend_factory=lambda: _Sentinel(),
        )
        out = gw.execute(GatewayRequest(
            gateway_name="sap_order", operation="validate",
            params={"case_id": "PO-9001"}, trace_id="t",
        ))
        assert out.status == "SUCCESS"
        assert out.data["stub_field"] == "stub-value"
        assert called["live"] == 0


class TestCanaryRouting:
    def test_zero_pct_stays_on_stub(self, monkeypatch, _clean_dlq, _clean_shadow):
        monkeypatch.setenv("ASOE_SAP_DRIVER", "s4hana")
        monkeypatch.setenv("ASOE_CANARY_PCT_SAP", "0.0")
        from gateways.sap_live import SapDomainGateway
        from contracts.models import GatewayRequest

        stub = _stub_for("sap_doc")

        class _NeverRuns:
            def execute(self, request):  # pragma: no cover
                raise AssertionError("0% canary must not invoke live")

        gw = SapDomainGateway(
            connector="sap_doc", stub=stub,
            live_backend_factory=lambda: _NeverRuns(),
        )
        out = gw.execute(GatewayRequest(
            gateway_name="sap_doc", operation="lookup",
            params={"case_id": "PO-9001"}, trace_id="t",
        ))
        assert out.status == "SUCCESS"
        assert out.data["stub_field"] == "stub-value"

    def test_100_pct_runs_live_and_records_diff(
        self, monkeypatch, _clean_dlq, _clean_shadow,
    ):
        monkeypatch.setenv("ASOE_SAP_DRIVER", "s4hana")
        monkeypatch.setenv("ASOE_CANARY_PCT_SAP", "1.0")
        from gateways.sap_live import SapDomainGateway
        from gateways.shadow_mode import get_diff_log
        from contracts.models import GatewayRequest, GatewayResponse

        stub = _stub_for("sap_order")

        class _Live:
            def execute(self, request):
                return GatewayResponse(
                    gateway_name="sap_order", operation="validate",
                    status="SUCCESS",
                    data={
                        "system": "S4H_PRD", "stub_field": "real-value",
                        "validation_status": "SO confirmed, ATP OK",
                        "sap_doc_number": "5100099999",
                    },
                )

        gw = SapDomainGateway(
            connector="sap_order", stub=stub,
            live_backend_factory=lambda: _Live(),
        )
        out = gw.execute(GatewayRequest(
            gateway_name="sap_order", operation="validate",
            params={"case_id": "PO-9001"}, trace_id="t",
        ))
        # Real authoritative.
        assert out.data["stub_field"] == "real-value"
        entries = get_diff_log()
        assert len(entries) == 1
        assert entries[0].connector == "sap_order"


class TestTerminalFailureGoesToDLQ:
    def test_live_failure_dlqs_with_sap_source(
        self, monkeypatch, _clean_dlq, _clean_shadow,
    ):
        monkeypatch.setenv("ASOE_SAP_DRIVER", "s4hana")
        monkeypatch.setenv("ASOE_CANARY_PCT_SAP", "1.0")
        from gateways.sap_live import SapDomainGateway
        from contracts.models import GatewayRequest
        from api import dead_letter_queue

        stub = _stub_for("sap_contract")

        class _AlwaysFails:
            def execute(self, request):
                raise RuntimeError("S/4HANA OData pool exhausted")

        gw = SapDomainGateway(
            connector="sap_contract", stub=stub,
            live_backend_factory=lambda: _AlwaysFails(),
        )
        out = gw.execute(GatewayRequest(
            gateway_name="sap_contract", operation="lookup",
            params={"case_id": "PO-9001", "tenant_id": "tenant-acme"},
            trace_id="t",
        ))
        # Caller sees the stub fallback.
        assert out.status == "SUCCESS"
        assert out.data["stub_field"] == "stub-value"
        entries = dead_letter_queue.list_for_tenant("tenant-acme")
        assert len(entries) == 1
        # DLQ source is "sap" — the OPERATOR-FACING category, not the
        # per-domain registry name. The operator dashboard groups every
        # SAP-driven orphan together.
        assert entries[0].source == "sap"
        assert entries[0].operation == "lookup"
        # The orphan payload carries `domain` so the operator dashboard
        # can pivot on (upstream system, SAP domain) without parsing
        # the operation name. Code-review MEDIUM finding.
        assert entries[0].payload["domain"] == "sap_contract"


class TestLiveBackendSurface:
    def test_live_backend_supports_each_domain(self, monkeypatch):
        monkeypatch.setenv("ASOE_SAP_HOST", "https://stub.example/")
        monkeypatch.setenv("ASOE_SAP_USER", "stub")
        monkeypatch.setenv("ASOE_SAP_PASSWORD", "stub")
        from gateways.sap_live import LiveSapBackend

        backend = LiveSapBackend()
        # The seven SAP domains all resolve as supported.
        assert backend.supports("sap_order", "validate")
        assert backend.supports("sap_doc", "lookup")
        assert backend.supports("sap_contract", "lookup")
        assert backend.supports("promotion", "lookup")
        assert backend.supports("sap_block", "lookup")
        assert backend.supports("sap_customer_master", "lookup")
        assert backend.supports("sla_contract", "lookup")
        # Unknown connector / operation: not supported.
        assert backend.supports("sap_order", "wishful_op") is False
        assert backend.supports("oms", "get_fulfillment_status") is False

    def test_live_backend_missing_creds_fails_loud(self, monkeypatch):
        monkeypatch.delenv("ASOE_SAP_HOST", raising=False)
        monkeypatch.delenv("ASOE_SAP_USER", raising=False)
        monkeypatch.delenv("ASOE_SAP_PASSWORD", raising=False)
        from gateways.sap_live import LiveSapBackend

        with pytest.raises(RuntimeError):
            LiveSapBackend()

    def test_live_execute_raises_off_live_mark(self, monkeypatch):
        monkeypatch.setenv("ASOE_SAP_HOST", "https://stub.example/")
        monkeypatch.setenv("ASOE_SAP_USER", "stub")
        monkeypatch.setenv("ASOE_SAP_PASSWORD", "stub")
        from gateways.sap_live import LiveSapBackend
        from contracts.models import GatewayRequest

        backend = LiveSapBackend()
        # Live transport raises on the red-green path; nightly -m live
        # mark is where the body lands.
        with pytest.raises(NotImplementedError):
            backend.execute(GatewayRequest(
                gateway_name="sap_order", operation="validate",
                params={}, trace_id="t",
            ))


class TestRegistrationName:
    def test_each_domain_preserves_its_registry_name(self, _clean_dlq):
        from gateways.sap_live import SapDomainGateway
        for connector in SAP_DOMAINS:
            gw = SapDomainGateway(
                connector=connector,
                stub=_stub_for(connector),
                live_backend_factory=lambda: None,
            )
            assert gw.name == connector
            assert gw.health_check() is True
