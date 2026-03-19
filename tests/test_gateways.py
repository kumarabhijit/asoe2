"""Tests for the gateways package — Protocol, Registry, Executor, Stub."""
from __future__ import annotations

import pytest

from contracts.models import GatewayRequest, GatewayResponse
from gateways.base import InfrastructureGateway
from gateways.registry import (
    register_gateway,
    get_gateway,
    registered_gateways,
    clear_registry,
)
from gateways.executor import GatewayExecutor
from gateways.stub import StubGateway


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure each test starts with an empty gateway registry."""
    clear_registry()
    yield
    clear_registry()


def _req(gateway: str = "sap", op: str = "read_price", **params) -> GatewayRequest:
    return GatewayRequest(
        gateway_name=gateway,
        operation=op,
        params=params,
        trace_id="t-001",
    )


# ---------------------------------------------------------------------------
# StubGateway
# ---------------------------------------------------------------------------


class TestStubGateway:
    def test_default_response_is_success(self):
        stub = StubGateway("sap")
        resp = stub.execute(_req())
        assert resp.status == "SUCCESS"
        assert resp.gateway_name == "sap"

    def test_canned_response_returned(self):
        canned = GatewayResponse(
            gateway_name="sap",
            operation="read_price",
            status="SUCCESS",
            data={"price": 100.0},
        )
        stub = StubGateway("sap", responses={"read_price": canned})
        resp = stub.execute(_req())
        assert resp.data == {"price": 100.0}

    def test_calls_recorded(self):
        stub = StubGateway("sap")
        stub.execute(_req(op="a"))
        stub.execute(_req(op="b"))
        assert len(stub.calls) == 2
        assert stub.calls[0].operation == "a"
        assert stub.calls[1].operation == "b"

    def test_health_check(self):
        assert StubGateway("sap").health_check() is True

    def test_satisfies_protocol(self):
        stub = StubGateway("sap")
        assert isinstance(stub, InfrastructureGateway)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestGatewayRegistry:
    def test_register_and_get(self):
        stub = StubGateway("sap")
        register_gateway(stub)
        assert get_gateway("sap") is stub

    def test_get_unknown_raises(self):
        with pytest.raises(KeyError, match="Gateway not registered"):
            get_gateway("nonexistent")

    def test_registered_gateways(self):
        register_gateway(StubGateway("sap"))
        register_gateway(StubGateway("db"))
        assert sorted(registered_gateways()) == ["db", "sap"]

    def test_clear_registry(self):
        register_gateway(StubGateway("sap"))
        clear_registry()
        assert registered_gateways() == []


# ---------------------------------------------------------------------------
# GatewayExecutor
# ---------------------------------------------------------------------------


class TestGatewayExecutor:
    def test_success_path(self):
        stub = StubGateway("sap", responses={
            "read_price": GatewayResponse(
                gateway_name="sap", operation="read_price",
                status="SUCCESS", data={"price": 99.0},
            ),
        })
        register_gateway(stub)
        resp = GatewayExecutor().run(_req())
        assert resp.status == "SUCCESS"
        assert resp.data == {"price": 99.0}
        assert resp.duration_ms is not None

    def test_unregistered_gateway_returns_unavailable(self):
        resp = GatewayExecutor().run(_req(gateway="missing"))
        assert resp.status == "UNAVAILABLE"
        assert "not registered" in resp.error

    def test_gateway_exception_returns_failed(self):
        class FailGateway:
            @property
            def name(self) -> str:
                return "fail"

            def execute(self, request: GatewayRequest) -> GatewayResponse:
                raise RuntimeError("connection refused")

            def health_check(self) -> bool:
                return False

        register_gateway(FailGateway())
        resp = GatewayExecutor().run(_req(gateway="fail"))
        assert resp.status == "FAILED"
        assert "connection refused" in resp.error
        assert resp.duration_ms is not None


# ---------------------------------------------------------------------------
# Contract models
# ---------------------------------------------------------------------------


class TestGatewayContracts:
    def test_request_extra_forbid(self):
        with pytest.raises(Exception):
            GatewayRequest(
                gateway_name="sap", operation="read",
                params={}, trace_id="t", unknown_field="x",
            )

    def test_response_extra_forbid(self):
        with pytest.raises(Exception):
            GatewayResponse(
                gateway_name="sap", operation="read",
                status="SUCCESS", unknown_field="x",
            )

    def test_response_status_literals(self):
        for status in ("SUCCESS", "FAILED", "TIMEOUT", "UNAVAILABLE"):
            resp = GatewayResponse(
                gateway_name="gw", operation="op", status=status,
            )
            assert resp.status == status
