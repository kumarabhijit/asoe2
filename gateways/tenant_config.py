from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from contracts.models import GatewayRequest, GatewayResponse
from recipes.DuplicatePORecipe import (
    WeightContractViolation,
    assert_weight_contract,
)


_LOGGER = logging.getLogger(__name__)

# Repo-relative path to the platform-default config seed. V1 reads this
# file on every construction; production-grade backing store (DB + API
# endpoints) deferred to A9 per ADR-030.
_PLATFORM_DEFAULTS_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "specs"
    / "duplicate-po"
    / "config-defaults.json"
)

# Ordered list of layer names — drives the merge order and the
# contribution-trace labels. Index = layer depth in the 5-level hierarchy
# (ADR-030).
_LAYER_NAMES: tuple[str, ...] = (
    "platform",   # L1 — config-defaults.json
    "tenant",     # L2 — tenant_config table (A9)
    "tier",       # L3 — customer_tier_overrides (V1 carries no weights)
    "customer",   # L4 — customer_specific_overrides; behavior_tag materialises here
    "channel",    # L5 — customer_channel_overrides (A9)
)


# ---------------------------------------------------------------------------
# Resolver — pure function with no I/O (the gateway class wraps it)
# ---------------------------------------------------------------------------


def resolve_weights(
    platform_weights: Dict[str, float],
    tenant_overrides: Dict[str, float],
    tier_overrides: Dict[str, float],
    customer_overrides: Dict[str, float],
    channel_overrides: Dict[str, float],
) -> tuple[Dict[str, float], List[Dict[str, Any]]]:
    """Layered merge of weight maps per ADR-029 §"Algorithm".

    Walks the hierarchy top-to-bottom, layering partial weight maps on
    top of inherited values. Records per-key the layer that supplied the
    final value, returning both the merged map and a contribution trace.

    Validation is the caller's responsibility — this function is
    deliberately pure so the gateway can catch ``WeightContractViolation``
    and decide between "succeed with merged" and "fall back to platform".

    Returns:
        (merged_weights, contribution_trace)

        contribution_trace is a list of dicts ordered by signal key:
            [{"signal": "po_number", "value": 0.30, "source_layer": "platform"}, ...]
    """
    merged: Dict[str, float] = {}
    source: Dict[str, str] = {}
    for layer_name, layer_map in zip(
        _LAYER_NAMES,
        (platform_weights, tenant_overrides, tier_overrides,
         customer_overrides, channel_overrides),
    ):
        for key, value in layer_map.items():
            merged[key] = value
            source[key] = layer_name

    contribution_trace: List[Dict[str, Any]] = [
        {"signal": key, "value": merged[key], "source_layer": source[key]}
        for key in sorted(merged.keys())
    ]
    return merged, contribution_trace


# ---------------------------------------------------------------------------
# Gateway adapter (InfrastructureGateway protocol)
# ---------------------------------------------------------------------------


class TenantConfigGateway:
    """File-backed config resolver for V1 (ADR-029, ADR-030).

    Responsibilities:
      1. Load platform defaults from ``config-defaults.json`` on disk.
      2. Resolve the 5-level hierarchy for a given event context.
      3. Validate the merged weight map; fall back to platform defaults
         on contract violation; emit structured warning for the alert
         pipeline.
      4. Return a typed ``GatewayResponse`` whose ``data`` field carries
         the resolved weights, the per-layer contribution trace, and the
         validation status (audit-chain consumers read this dict).

    Production-grade backing store (tenant_config table + API endpoints
    + ConfigChange events) lands in A9 — see ADR-030 §V1 / V1.5.
    """

    def __init__(self, defaults_path: Optional[Path] = None) -> None:
        path = defaults_path or _PLATFORM_DEFAULTS_PATH
        self._defaults_path = path
        self._defaults = self._load_defaults(path)

    # -- protocol surface -----------------------------------------------------

    @property
    def name(self) -> str:
        return "tenant_config"

    def health_check(self) -> bool:
        return self._defaults_path.exists()

    def execute(self, request: GatewayRequest) -> GatewayResponse:
        """Resolve config for the requested event scope.

        ``request.params`` keys (all optional except tenant_id):
          tenant_id        (str, required)
          customer_id      (str)
          customer_tier    ("strategic" | "standard" | "smb")
          channel          (str — provider-defined; e.g. "EDI", "PORTAL")
          behavior_tag     ("blanket_po" | "drop_ship" | "high_frequency")

        ``response.data`` always contains:
          weights              — resolved ``Dict[str, float]``
          contribution_trace   — per-signal source-layer trace
          validation_status    — "ok" | "fallback_to_platform"
          violation_reason     — Optional[str], present only on fallback
          rejected_layered_config — Optional[Dict[str, float]], the merged
                                    map that failed validation (for audit)
          scope                — echoed (tenant_id, customer_id, ...) for trace
        """
        params = request.params or {}
        tenant_id = params.get("tenant_id")
        customer_id = params.get("customer_id")
        customer_tier = params.get("customer_tier")
        channel = params.get("channel")
        behavior_tag = params.get("behavior_tag")

        platform_weights = dict(self._defaults["score_weights"])
        tenant_overrides: Dict[str, float] = {}      # L2 — A9 will populate
        tier_overrides: Dict[str, float] = {}        # L3 — V1 file has no tier weights
        customer_overrides = self._customer_overrides_from_behavior(behavior_tag)
        channel_overrides: Dict[str, float] = {}     # L5 — A9 will populate

        merged, trace = resolve_weights(
            platform_weights=platform_weights,
            tenant_overrides=tenant_overrides,
            tier_overrides=tier_overrides,
            customer_overrides=customer_overrides,
            channel_overrides=channel_overrides,
        )

        scope = {
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "customer_tier": customer_tier,
            "channel": channel,
            "behavior_tag": behavior_tag,
        }

        try:
            assert_weight_contract(merged)
        except WeightContractViolation as exc:
            # Fail-closed: log + return platform defaults. Recipe runs with
            # safe weights; admin sees the alert + audit-chain entry.
            _LOGGER.warning(
                "config_validation_alert: WEIGHT_CONTRACT_VIOLATION — %s",
                exc,
                extra={
                    "event_type": "config_validation_alert",
                    "severity": "ERROR",
                    "scope": scope,
                    "rejected_layered_config": merged,
                },
            )
            platform_only_trace = [
                {"signal": key, "value": platform_weights[key], "source_layer": "platform"}
                for key in sorted(platform_weights.keys())
            ]
            return GatewayResponse(
                gateway_name=self.name,
                operation=request.operation,
                status="SUCCESS",  # gateway succeeded — the fall-back is by design
                data={
                    "weights": platform_weights,
                    "contribution_trace": platform_only_trace,
                    "validation_status": "fallback_to_platform",
                    "violation_reason": str(exc),
                    "rejected_layered_config": merged,
                    "scope": scope,
                },
            )

        return GatewayResponse(
            gateway_name=self.name,
            operation=request.operation,
            status="SUCCESS",
            data={
                "weights": merged,
                "contribution_trace": trace,
                "validation_status": "ok",
                "violation_reason": None,
                "rejected_layered_config": None,
                "scope": scope,
            },
        )

    # -- internals ------------------------------------------------------------

    def _customer_overrides_from_behavior(
        self,
        behavior_tag: Optional[str],
    ) -> Dict[str, float]:
        """Materialise customer_behavior_overrides into an L4 partial map.

        Per the _meta block in config-defaults.json and ADR-029, behavior
        tags are not a sixth layer — admin tooling tags a customer with
        a behavior, and the behavior's score_weights_override becomes the
        customer-specific (L4) override. V1 file-backed resolver
        replicates that materialisation directly.
        """
        if not behavior_tag:
            return {}
        behavior_overrides = self._defaults.get("customer_behavior_overrides", {})
        entry = behavior_overrides.get(behavior_tag)
        if not entry:
            return {}
        # Float-cast defensively — JSON gives us numbers, but typed
        # downstream consumers expect floats.
        return {
            key: float(value)
            for key, value in entry.get("score_weights_override", {}).items()
        }

    @staticmethod
    def _load_defaults(path: Path) -> Dict[str, Any]:
        """Load config-defaults.json and return the duplicate_detection block.

        The wider JSON document carries an ``_meta`` block (REFERENCE
        annotations) and the ``duplicate_detection`` payload. We extract
        the latter so callers see a flat shape.
        """
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw["duplicate_detection"]
