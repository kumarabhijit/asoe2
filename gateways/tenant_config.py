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

# Repo-relative path to the platform-default config seed. The platform
# layer (L1) always reads from disk; layers 2-5 come from the DB when
# a TenantConfigRepository is provided to the gateway.
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
    "tier",       # L3 — tenant_config table (A9)
    "customer",   # L4 — tenant_config table OR behavior_tag materialisation
    "channel",    # L5 — tenant_config table (A9)
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
    """Layered merge of weight maps per ADR-029 §\"Algorithm\".

    Walks the hierarchy top-to-bottom, layering partial weight maps on
    top of inherited values. Records per-key the layer that supplied the
    final value, returning both the merged map and a contribution trace.

    Validation is the caller's responsibility — this function is
    deliberately pure so the gateway can catch ``WeightContractViolation``
    and decide between \"succeed with merged\" and \"fall back to platform\".

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
    """Config resolver for ADR-029 / ADR-030.

    Two operating modes:
      * **File-only** (V1 baseline) — when ``repository`` is None,
        layers 2-5 are empty unless ``behavior_tag`` materialises into
        the customer layer. Mirrors the original V1 file-backed
        implementation; preserved so tests / setups that don't init the
        DB keep working unchanged.
      * **DB-backed** (V1.5 / A9) — when ``repository`` is a
        ``TenantConfigRepository``, layers 2-5 are read from the
        ``tenant_config`` table via ``resolve_layered_overrides``.
        DB customer rows take precedence over ``behavior_tag``
        materialisation; the materialisation kicks in only when no DB
        customer row exists for the inbound (tenant, customer_id)
        scope.

    Validation runs on the merged map; on ``WeightContractViolation``
    the gateway falls back to platform defaults and surfaces the
    violation in ``response.data`` for audit consumers.
    """

    def __init__(
        self,
        defaults_path: Optional[Path] = None,
        repository: Optional[Any] = None,
    ) -> None:
        path = defaults_path or _PLATFORM_DEFAULTS_PATH
        self._defaults_path = path
        self._defaults = self._load_defaults(path)
        # Optional — typed loosely (Any) to avoid an import cycle with
        # db/repository.py. Expected to expose the
        # ``resolve_layered_overrides(tenant_id, customer_tier=..., 
        # customer_id=..., channel=...) -> Dict[layer_name, Dict[str, float]]``
        # protocol.
        self._repository = repository

    # -- protocol surface -----------------------------------------------------

    @property
    def name(self) -> str:
        return "tenant_config"

    def health_check(self) -> bool:
        return self._defaults_path.exists()

    def execute(self, request: GatewayRequest) -> GatewayResponse:
        """Resolve config for the requested event scope.

        ``request.params`` keys (all optional except tenant_id when the
        gateway is DB-backed):
          tenant_id        (str, required for DB-backed mode)
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

        if self._repository is not None and tenant_id:
            db_layers = self._repository.resolve_layered_overrides(
                tenant_id=tenant_id,
                customer_tier=customer_tier,
                customer_id=customer_id,
                channel=channel,
            )
            tenant_overrides = db_layers["tenant"]
            tier_overrides = db_layers["tier"]
            # DB customer-layer wins over behavior_tag materialisation
            # (admin explicitly wrote the customer override). Fall back
            # to behavior_tag-derived map only when no DB customer row
            # exists — preserves V1 file-only semantics for tenants
            # that haven't migrated to DB-backed admin tooling yet.
            customer_overrides = (
                db_layers["customer"]
                if db_layers["customer"]
                else self._customer_overrides_from_behavior(behavior_tag)
            )
            channel_overrides = db_layers["channel"]
        else:
            tenant_overrides: Dict[str, float] = {}
            tier_overrides: Dict[str, float] = {}
            customer_overrides = self._customer_overrides_from_behavior(behavior_tag)
            channel_overrides: Dict[str, float] = {}

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
            # Fail-closed: log + return platform defaults. Recipe runs
            # with safe weights; admin sees the alert + audit-chain
            # entry.
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
        replicates that materialisation directly. PR-C.2 keeps it as a
        fallback so events whose tenant has no DB customer-row yet
        still benefit from the admin-configured behaviour vocabulary.
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
