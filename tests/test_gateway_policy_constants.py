"""PARITY-6 — per-gateway timeout + budget constants.

The Integration review requires explicit per-gateway timeout + budget
config in ``contracts/policy.py`` BEFORE any real-connector sub-phase
ships, so a Graph 429 or SAP fan-out exhaustion can't silently extend
the worker request budget.

Contract:

  * ``GATEWAY_TIMEOUT_S`` is a typed dict keyed by gateway name with
    the per-gateway request timeout in seconds.
  * Required entries: ``graph``, ``sap``, ``oms``, ``document_extraction``.
  * Each value is a positive float <= ``GATEWAY_CIRCUIT_BREAKER_P95_LATENCY_S``
    (you can't time out longer than the breaker would trip anyway).
"""

from __future__ import annotations

import pytest


class TestGatewayTimeouts:
    def test_per_gateway_timeout_map_exists(self):
        from contracts.policy import GATEWAY_TIMEOUT_S

        assert isinstance(GATEWAY_TIMEOUT_S, dict)
        for required in ("graph", "sap", "oms", "document_extraction"):
            assert required in GATEWAY_TIMEOUT_S, (
                f"GATEWAY_TIMEOUT_S missing required entry {required!r}"
            )

    def test_timeouts_are_positive_floats_under_breaker_p95(self):
        from contracts.policy import (
            GATEWAY_TIMEOUT_S,
            GATEWAY_CIRCUIT_BREAKER_P95_LATENCY_S,
        )

        for name, value in GATEWAY_TIMEOUT_S.items():
            assert isinstance(value, (int, float)), name
            assert value > 0, f"{name} timeout must be positive (got {value})"
            assert value <= GATEWAY_CIRCUIT_BREAKER_P95_LATENCY_S, (
                f"{name} timeout {value}s exceeds breaker p95 "
                f"{GATEWAY_CIRCUIT_BREAKER_P95_LATENCY_S}s — the breaker "
                f"would trip before the request times out"
            )

    def test_graph_timeout_approximately_3s(self):
        """Per the parity plan: Graph ~3s budget."""
        from contracts.policy import GATEWAY_TIMEOUT_S

        assert 2.0 <= GATEWAY_TIMEOUT_S["graph"] <= 5.0

    def test_sap_timeout_approximately_8s(self):
        """Per the parity plan: SAP ~8s budget."""
        from contracts.policy import GATEWAY_TIMEOUT_S

        assert 5.0 <= GATEWAY_TIMEOUT_S["sap"] <= 10.0

    def test_executor_consults_per_gateway_budget(self):
        """Regression for code-review MEDIUM finding: the map is config
        only if the executor doesn't read it. This test asserts the
        wiring."""
        from gateways.executor import GatewayExecutor
        from contracts.models import GatewayRequest
        from contracts.policy import GATEWAY_TIMEOUT_S
        from gateways.registry import register_gateway
        from gateways.stub import StubGateway
        from contracts.models import GatewayResponse
        import time

        # Stub gateway that sleeps long enough to exceed the graph
        # per-gateway budget (3s) but not the GatewayRequest default
        # (5s). With wiring the request must TIMEOUT.
        class SlowGateway:
            name = "graph"

            def execute(self, request):
                time.sleep(GATEWAY_TIMEOUT_S["graph"] + 1.0)
                return GatewayResponse(
                    gateway_name="graph",
                    operation=request.operation,
                    status="SUCCESS",
                )

        # Wire via register_gateway. Use a low-cost name we'll clean up.
        from gateways.registry import _GATEWAY_REGISTRY
        prev = dict(_GATEWAY_REGISTRY)
        try:
            _GATEWAY_REGISTRY["graph"] = SlowGateway()
            executor = GatewayExecutor()
            req = GatewayRequest(
                gateway_name="graph",
                operation="list_messages",
                params={},
                trace_id="t-abc",
                # Use the default 5000ms so the executor SHOULD apply
                # the 3000ms graph budget.
            )
            resp = executor.run(req)
            assert resp.status == "TIMEOUT"
        finally:
            _GATEWAY_REGISTRY.clear()
            _GATEWAY_REGISTRY.update(prev)


class TestShadowModeThresholds:
    """Decision Q9 — shadow-mode acceptance thresholds.

    Codified as a YAML file so a non-engineer compliance reviewer can
    diff a row when a per-tenant threshold is loosened.
    """

    def test_thresholds_file_exists(self):
        from pathlib import Path
        repo_root = Path(__file__).resolve().parent.parent
        path = repo_root / "tests" / "eval" / "shadow_mode_thresholds.yaml"
        assert path.exists()

    def test_audit_bearing_fields_zero_diff(self):
        from tests.eval._shadow_mode_loader import load_thresholds

        thresholds = load_thresholds()
        assert thresholds["audit_bearing"]["max_diff_pct"] == 0.0

    def test_derived_fields_max_5pct(self):
        from tests.eval._shadow_mode_loader import load_thresholds

        thresholds = load_thresholds()
        assert thresholds["derived"]["max_diff_pct"] <= 5.0

    def test_window_is_seven_days(self):
        from tests.eval._shadow_mode_loader import load_thresholds

        thresholds = load_thresholds()
        assert thresholds["window_days"] == 7

    def test_audit_bearing_diff_rate_at_most_one_pct(self):
        from tests.eval._shadow_mode_loader import load_thresholds

        thresholds = load_thresholds()
        # Decision Q9: < 1% — we encode the exact upper bound (1.0%)
        # in the YAML so the comparison is "≤ 1.0".
        assert thresholds["audit_bearing"]["max_diff_rate_pct"] <= 1.0


class TestDeadLetterQueue:
    """A poison-message DLQ surface for downstream connectors. The
    handler interface lives in ``api.dead_letter_queue``; specific
    connectors (Graph, SAP, OMS) push onto it on terminal failure."""

    def test_dlq_records_message_and_reason(self):
        from api import dead_letter_queue as dlq
        dlq.reset()

        dlq.record(
            source="graph",
            operation="list_messages",
            tenant_id="t-1",
            reason="429_rate_limit_after_retries",
            payload={"folder": "Inbox"},
        )
        entries = dlq.list_for_tenant("t-1")
        assert len(entries) == 1
        assert entries[0].source == "graph"
        assert entries[0].reason.startswith("429")

    def test_dlq_is_tenant_scoped(self):
        from api import dead_letter_queue as dlq
        dlq.reset()
        dlq.record(source="sap", operation="read_order", tenant_id="t-A",
                   reason="pool_exhausted", payload={})
        dlq.record(source="sap", operation="read_order", tenant_id="t-B",
                   reason="pool_exhausted", payload={})
        assert len(dlq.list_for_tenant("t-A")) == 1
        assert len(dlq.list_for_tenant("t-B")) == 1


class TestEgressPIIRedaction:
    """Per Security + ML review: PII redaction on egress to AzureDI.
    Mask customer credit fields, addresses outside the field-of-
    interest, before sending."""

    def test_credit_fields_masked(self):
        from gateways.azure_di_egress_redaction import redact_for_azure_di

        body = "Customer credit limit: $100,000. SSN: 123-45-6789. Address: 1 Main St"
        out = redact_for_azure_di(body)
        assert "123-45-6789" not in out
        assert "$100,000" not in out

    def test_idempotent(self):
        from gateways.azure_di_egress_redaction import redact_for_azure_di

        body = "no pii here"
        assert redact_for_azure_di(body) == body

    def test_grouped_cc_redacted(self):
        """The classic 4-4-4-4 grouped credit-card format."""
        from gateways.azure_di_egress_redaction import redact_for_azure_di

        body = "card on file: 4242 4242 4242 4242"
        out = redact_for_azure_di(body)
        assert "4242 4242 4242 4242" not in out
        assert "<redacted-cc>" in out

    def test_contiguous_cc_with_valid_luhn_redacted(self):
        """A contiguous 16-digit number that passes Luhn IS a CC; redact."""
        from gateways.azure_di_egress_redaction import redact_for_azure_di

        # 4111111111111111 is a Visa test number that passes Luhn.
        body = "charge: 4111111111111111"
        out = redact_for_azure_di(body)
        assert "4111111111111111" not in out

    def test_innocent_digit_run_not_over_redacted(self):
        """Regression for code-review HIGH finding: a 13-19-digit
        sequence that ISN'T a real CC (fails Luhn) MUST NOT be flagged.
        Over-redaction breaks AzureDI's spatial-extraction anchoring
        on the redacted spans."""
        from gateways.azure_di_egress_redaction import redact_for_azure_di

        # Random 16-digit run that almost certainly fails Luhn.
        body = "order id: 1234567890123456 PO confirmation"
        out = redact_for_azure_di(body)
        # 1234567890123456 fails Luhn → not redacted.
        assert "1234567890123456" in out

    def test_phone_number_list_not_over_redacted(self):
        """The original loose CC regex matched separator-rich digit
        sequences like a multi-phone list. The Luhn check prevents this."""
        from gateways.azure_di_egress_redaction import redact_for_azure_di

        body = "contact list: 415-555-1234, 415-555-5678, 415-555-9012"
        out = redact_for_azure_di(body)
        # The phones themselves stay (egress redaction is for financial
        # PII; phone scrubbing is the log_redaction module's job).
        assert "415-555-1234" in out
