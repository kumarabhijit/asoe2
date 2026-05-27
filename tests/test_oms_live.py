"""PARITY-6.4 — OMS live backend.

Same routing pattern as Graph 6.1 + SAP 6.3 — the new wrinkle is the
**post-recipe-success failure** contract: if a recipe completes
GREEN against the shadow + canary plan but the OMS write
subsequently fails, the orphan must land in
``api.dead_letter_queue.record(source='oms', ...)`` so the operator
dashboard surfaces the SOX-relevant mismatch (the audit log says
'order accepted' but the downstream system never wrote it).
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


def _stub():
    from gateways.stub import StubGateway
    from contracts.models import GatewayResponse
    return StubGateway(
        "oms",
        responses={
            "get_fulfillment_status": GatewayResponse(
                gateway_name="oms", operation="get_fulfillment_status",
                status="SUCCESS", data={"fulfilled": False},
            ),
            "write_order_acceptance": GatewayResponse(
                gateway_name="oms", operation="write_order_acceptance",
                status="SUCCESS", data={"oms_order_id": "OMS-STUB-1"},
            ),
        },
    )


class TestDefaultDriverIsRecorded:
    def test_default_routes_to_stub(self, monkeypatch, _clean_dlq):
        monkeypatch.delenv("ASOE_OMS_DRIVER", raising=False)
        from gateways.oms_live import OmsGateway
        from contracts.models import GatewayRequest

        class _Sentinel:
            def execute(self, request):  # pragma: no cover
                raise AssertionError("live OMS must not be called by default")

        gw = OmsGateway(stub=_stub(), live_backend_factory=lambda: _Sentinel())
        out = gw.execute(GatewayRequest(
            gateway_name="oms", operation="get_fulfillment_status",
            params={"case_id": "PO-9001"}, trace_id="t",
        ))
        assert out.status == "SUCCESS"
        assert out.data == {"fulfilled": False}


class TestCanaryRouting:
    def test_100_pct_runs_live(self, monkeypatch, _clean_dlq, _clean_shadow):
        monkeypatch.setenv("ASOE_OMS_DRIVER", "live")
        monkeypatch.setenv("ASOE_CANARY_PCT_OMS", "1.0")
        from gateways.oms_live import OmsGateway
        from contracts.models import GatewayRequest, GatewayResponse

        class _Live:
            def execute(self, request):
                return GatewayResponse(
                    gateway_name="oms", operation="get_fulfillment_status",
                    status="SUCCESS", data={"fulfilled": True},
                )

        gw = OmsGateway(stub=_stub(), live_backend_factory=lambda: _Live())
        out = gw.execute(GatewayRequest(
            gateway_name="oms", operation="get_fulfillment_status",
            params={"case_id": "PO-9001"}, trace_id="t",
        ))
        assert out.data["fulfilled"] is True


class TestTerminalReadFailureGoesToDLQ:
    def test_read_failure_dlqs_with_oms_source(
        self, monkeypatch, _clean_dlq, _clean_shadow,
    ):
        monkeypatch.setenv("ASOE_OMS_DRIVER", "live")
        monkeypatch.setenv("ASOE_CANARY_PCT_OMS", "1.0")
        from gateways.oms_live import OmsGateway
        from contracts.models import GatewayRequest
        from api import dead_letter_queue

        class _Fails:
            def execute(self, request):
                raise RuntimeError("OMS 503")

        gw = OmsGateway(stub=_stub(), live_backend_factory=lambda: _Fails())
        out = gw.execute(GatewayRequest(
            gateway_name="oms", operation="get_fulfillment_status",
            params={"case_id": "PO-9001", "tenant_id": "tenant-acme"},
            trace_id="t",
        ))
        # Stub fallback so the recipe completes.
        assert out.status == "SUCCESS"
        entries = dead_letter_queue.list_for_tenant("tenant-acme")
        assert len(entries) == 1
        assert entries[0].source == "oms"


class TestPostRecipeSuccessFailureContract:
    """The plan's PARITY-6.4 §: 'On failure post-recipe-success, call
    api.dead_letter_queue.record(source="oms", ...)' so the orphan is
    visible. This is the OMS-specific risk — every other connector is
    read-only, so the post-recipe-success window doesn't exist."""

    def test_record_oms_write_orphan_helper(self, _clean_dlq):
        from gateways.oms_live import record_post_success_orphan
        from api import dead_letter_queue

        record_post_success_orphan(
            tenant_id="tenant-eu",
            operation="write_order_acceptance",
            recipe_run_id="run-42",
            reason="OMS 502 after retry exhausted",
        )
        entries = dead_letter_queue.list_for_tenant("tenant-eu")
        assert len(entries) == 1
        assert entries[0].source == "oms"
        assert entries[0].operation == "write_order_acceptance"
        # The orphan carries enough context to correlate with the
        # recipe run that the auditor will reference.
        assert entries[0].payload.get("recipe_run_id") == "run-42"
        assert "post-recipe-success" in entries[0].reason.lower()

    def test_record_helper_requires_tenant(self, _clean_dlq):
        from gateways.oms_live import record_post_success_orphan
        with pytest.raises(ValueError):
            record_post_success_orphan(
                tenant_id="", operation="write_order_acceptance",
                recipe_run_id="run-42", reason="x",
            )


class TestLiveBackendSurface:
    def test_live_backend_missing_creds_fails_loud(self, monkeypatch):
        monkeypatch.delenv("ASOE_OMS_BASE_URL", raising=False)
        monkeypatch.delenv("ASOE_OMS_API_KEY", raising=False)
        from gateways.oms_live import LiveOmsBackend
        with pytest.raises(RuntimeError):
            LiveOmsBackend()

    def test_live_execute_raises_off_live_mark(self, monkeypatch):
        monkeypatch.setenv("ASOE_OMS_BASE_URL", "https://stub.example/")
        monkeypatch.setenv("ASOE_OMS_API_KEY", "stub")
        from gateways.oms_live import LiveOmsBackend
        from contracts.models import GatewayRequest
        backend = LiveOmsBackend()
        with pytest.raises(NotImplementedError):
            backend.execute(GatewayRequest(
                gateway_name="oms", operation="get_fulfillment_status",
                params={}, trace_id="t",
            ))


class TestRegistrationName:
    def test_gateway_registers_under_oms(self, _clean_dlq):
        from gateways.oms_live import OmsGateway
        gw = OmsGateway(stub=_stub(), live_backend_factory=lambda: None)
        assert gw.name == "oms"
        assert gw.health_check() is True
