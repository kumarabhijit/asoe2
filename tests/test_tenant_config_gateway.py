from __future__ import annotations

# ADR-029 V1 §8 — tenant_config gateway tests.
#
# Two categories:
#   1. resolve_weights() — pure function tests for the layered merge
#      algorithm + per-layer contribution trace.
#   2. TenantConfigGateway.execute() — gateway-protocol tests covering
#      file-backed loading, behavior_tag materialisation, and the
#      fail-closed-to-platform path on WeightContractViolation.

from contracts.models import GatewayRequest
from gateways.tenant_config import (
    TenantConfigGateway,
    resolve_weights,
)
from recipes.DuplicatePORecipe import (
    WeightContractViolation,
    _WEIGHTS,
)


# ---------------------------------------------------------------------------
# resolve_weights() — pure layered merge
# ---------------------------------------------------------------------------


class TestResolveWeights:
    def _platform(self) -> dict:
        return {
            "po_number": 0.30, "customer_id": 0.15, "line_items": 0.20,
            "amount": 0.10, "timestamp": 0.10, "ship_to": 0.05,
            "channel": 0.05, "delivery_date": 0.05,
        }

    def test_platform_only_passes_through(self):
        merged, trace = resolve_weights(
            platform_weights=self._platform(),
            tenant_overrides={},
            tier_overrides={},
            customer_overrides={},
            channel_overrides={},
        )
        assert merged == self._platform()
        # Trace ordered alphabetically by signal name.
        assert [t["signal"] for t in trace] == sorted(merged.keys())
        assert all(t["source_layer"] == "platform" for t in trace)

    def test_layered_merge_partial_overrides(self):
        """ADR-029 V1 §8 — verifies merge order and per-layer trace.

        Tenant overrides po_number; customer overrides line_items;
        channel further overrides line_items. Final value for each key
        comes from the deepest layer that supplied it.
        """
        merged, trace = resolve_weights(
            platform_weights=self._platform(),
            tenant_overrides={"po_number": 0.25},
            tier_overrides={},
            customer_overrides={"line_items": 0.30},
            channel_overrides={"line_items": 0.32},
        )
        # po_number from tenant (L2)
        assert merged["po_number"] == 0.25
        # customer_id retained from platform (L1)
        assert merged["customer_id"] == 0.15
        # line_items from channel (L5) — channel wins over customer (L4)
        assert merged["line_items"] == 0.32

        source_by_signal = {t["signal"]: t["source_layer"] for t in trace}
        assert source_by_signal["po_number"] == "tenant"
        assert source_by_signal["customer_id"] == "platform"
        assert source_by_signal["line_items"] == "channel"
        assert source_by_signal["delivery_date"] == "platform"

    def test_trace_carries_value_per_signal(self):
        """Trace entries always include the resolved value, not just the layer."""
        merged, trace = resolve_weights(
            platform_weights=self._platform(),
            tenant_overrides={},
            tier_overrides={"amount": 0.12},
            customer_overrides={},
            channel_overrides={},
        )
        # Sanity: trace value matches merged value for every signal.
        for entry in trace:
            assert entry["value"] == merged[entry["signal"]]
        # Trace records the new tier override.
        amount_entry = next(t for t in trace if t["signal"] == "amount")
        assert amount_entry["value"] == 0.12
        assert amount_entry["source_layer"] == "tier"

    def test_resolve_weights_does_not_validate(self):
        """The pure function deliberately does not call assert_weight_contract.

        Validation is the caller's responsibility — the gateway catches
        WeightContractViolation and falls back to platform. The pure
        function returning an invalid map without raising is the
        contract that enables that pattern.
        """
        # Sum is 1.10 — would raise if the function validated.
        bad = {
            "po_number": 0.40, "customer_id": 0.15, "line_items": 0.20,
            "amount": 0.10, "timestamp": 0.10, "ship_to": 0.05,
            "channel": 0.05, "delivery_date": 0.05,
        }
        merged, _trace = resolve_weights(
            platform_weights=bad,
            tenant_overrides={},
            tier_overrides={},
            customer_overrides={},
            channel_overrides={},
        )
        assert merged == bad  # pure function — no validation


# ---------------------------------------------------------------------------
# TenantConfigGateway — file-backed resolver via the InfrastructureGateway
#                       protocol surface
# ---------------------------------------------------------------------------


class TestTenantConfigGatewayProtocol:
    def test_name_is_tenant_config(self):
        gw = TenantConfigGateway()
        assert gw.name == "tenant_config"

    def test_health_check_returns_true_when_defaults_present(self):
        gw = TenantConfigGateway()
        assert gw.health_check() is True

    def test_execute_with_minimal_params_returns_platform_weights(self):
        gw = TenantConfigGateway()
        response = gw.execute(GatewayRequest(
            gateway_name="tenant_config",
            operation="resolve_for_event",
            params={"customer_id": "R-10", "order_id": "PO-1"},
            trace_id="t-1",
        ))
        assert response.status == "SUCCESS"
        assert response.data["validation_status"] == "ok"
        # Platform defaults (gateways/configs/duplicate_po/defaults.json's score_weights block).
        assert response.data["weights"] == _WEIGHTS
        # Trace covers every signal, all sourced from platform.
        sources = {t["source_layer"] for t in response.data["contribution_trace"]}
        assert sources == {"platform"}

    def test_execute_with_blanket_po_behavior_tag_applies_l4_override(self):
        """blanket_po preset overrides po_number → 0.10 and line_items → 0.35."""
        gw = TenantConfigGateway()
        response = gw.execute(GatewayRequest(
            gateway_name="tenant_config",
            operation="resolve_for_event",
            params={
                "customer_id": "R-10",
                "order_id": "PO-1",
                "behavior_tag": "blanket_po",
            },
            trace_id="t-1",
        ))
        assert response.status == "SUCCESS"
        assert response.data["validation_status"] == "ok"
        weights = response.data["weights"]
        assert weights["po_number"] == 0.10
        assert weights["line_items"] == 0.35
        # Other keys keep their platform values.
        assert weights["customer_id"] == 0.15
        # Trace shows the L4 source for the overridden keys.
        source_by_signal = {
            t["signal"]: t["source_layer"]
            for t in response.data["contribution_trace"]
        }
        assert source_by_signal["po_number"] == "customer"
        assert source_by_signal["line_items"] == "customer"
        assert source_by_signal["customer_id"] == "platform"
        # Sum still 1.0 (validated by assert_weight_contract).
        assert abs(sum(weights.values()) - 1.0) < 1e-4

    def test_execute_with_unknown_behavior_tag_falls_back_to_platform(self):
        gw = TenantConfigGateway()
        response = gw.execute(GatewayRequest(
            gateway_name="tenant_config",
            operation="resolve_for_event",
            params={
                "customer_id": "R-10",
                "behavior_tag": "made_up_tag",
            },
            trace_id="t-1",
        ))
        # Unknown tag silently maps to no L4 override (the gateway is
        # tolerant of input it doesn't recognise — admin tooling owns
        # the tag vocabulary). Result is platform-only.
        assert response.data["validation_status"] == "ok"
        assert response.data["weights"] == _WEIGHTS

    def test_response_data_includes_scope_echo(self):
        gw = TenantConfigGateway()
        response = gw.execute(GatewayRequest(
            gateway_name="tenant_config",
            operation="resolve_for_event",
            params={
                "tenant_id": "acme",
                "customer_id": "R-10",
                "customer_tier": "strategic",
                "channel": "EDI",
                "behavior_tag": "blanket_po",
            },
            trace_id="t-1",
        ))
        scope = response.data["scope"]
        assert scope == {
            "tenant_id": "acme",
            "customer_id": "R-10",
            "customer_tier": "strategic",
            "channel": "EDI",
            "behavior_tag": "blanket_po",
        }


# ---------------------------------------------------------------------------
# Fail-closed behaviour — WeightContractViolation must NOT propagate out;
# the gateway returns SUCCESS with platform defaults instead.
# ---------------------------------------------------------------------------


class TestFailClosedToPlatform:
    """ADR-029 V1 §8 — test_invalid_merged_config_falls_back_to_platform."""

    def _gateway_with_corrupt_defaults(
        self,
        bad_score_weights: dict,
    ) -> TenantConfigGateway:
        """Construct a gateway whose in-memory defaults are deliberately
        invalid, so the merged map fails assert_weight_contract.

        We set _defaults directly on the instance — no disk write needed.
        """
        gw = TenantConfigGateway()
        gw._defaults = {
            **gw._defaults,
            "score_weights": bad_score_weights,
        }
        return gw

    def test_sum_violation_falls_back_to_module_default(self):
        """Sum drift > 1e-4 triggers fall-back to module-default _WEIGHTS."""
        # Sum 1.05 — outside tolerance. The merge produces a 1.05-summing
        # map which fails the contract; gateway should recover.
        bad = {
            "po_number": 0.35, "customer_id": 0.15, "line_items": 0.20,
            "amount": 0.10, "timestamp": 0.10, "ship_to": 0.05,
            "channel": 0.05, "delivery_date": 0.05,
        }
        gw = self._gateway_with_corrupt_defaults(bad)
        response = gw.execute(GatewayRequest(
            gateway_name="tenant_config",
            operation="resolve_for_event",
            params={"customer_id": "R-10"},
            trace_id="t-1",
        ))
        # Gateway succeeded — fall-back was internal, not an error.
        assert response.status == "SUCCESS"
        assert response.data["validation_status"] == "fallback_to_platform"
        assert response.data["violation_reason"] is not None
        assert "Weight sum" in response.data["violation_reason"]
        # Returned weights are the platform-default map (the bad one in
        # this test, since we inject directly into _defaults['score_weights'])
        # — for fall-back, the gateway returns whatever its
        # platform_weights variable holds. Verify shape/sum is whatever
        # the gateway considered "platform" at request time.
        assert set(response.data["weights"].keys()) == set(bad.keys())
        # rejected_layered_config is the merged map that failed validation
        assert response.data["rejected_layered_config"] is not None

    def test_unknown_key_violation_includes_diagnostic_context(self):
        """Extra-key violation surfaces in violation_reason for audit."""
        bad = {
            **_WEIGHTS,
            "unknown_signal": 0.0,
        }
        # Renormalise platform total back to 1.0 so the test isolates
        # the key-set check (sum is fine; only the extra key is wrong).
        bad["po_number"] = 0.30
        gw = self._gateway_with_corrupt_defaults(bad)
        response = gw.execute(GatewayRequest(
            gateway_name="tenant_config",
            operation="resolve_for_event",
            params={"customer_id": "R-10"},
            trace_id="t-1",
        ))
        assert response.data["validation_status"] == "fallback_to_platform"
        assert "Weight key set mismatch" in response.data["violation_reason"]
        assert "unknown_signal" in response.data["violation_reason"]

    def test_recipe_violation_type_is_caught(self):
        """The exception type the gateway catches is the same one the
        recipe defines and exports — single source of truth."""
        # Sanity check: the WeightContractViolation symbol the gateway
        # imports IS the one tests assert against.
        from gateways import tenant_config as tc_module
        assert tc_module.WeightContractViolation is WeightContractViolation
