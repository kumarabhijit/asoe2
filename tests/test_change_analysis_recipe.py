"""Pure-function unit tests for the Change Analysis recipe (ADR-042 Phase 6).

`recipes/ChangeAnalysisRecipe.evaluate_change` is the deterministic, recipe-homed
constraint evaluator (NOT in `constraints/`). These tests lock variable
cardinality (only signalled constraints evaluate), the always-on Financial check
+ cosign gate from policy, the scenario/decision derivation, determinism, and
the round-trip into the `ChangeAnalysis` contract. No I/O, no LLM.
"""

from __future__ import annotations

from api.schemas import ChangeAnalysis
from contracts.policy import HIGH_VALUE_OVERRIDE_THRESHOLD_USD
from recipes.ChangeAnalysisRecipe import (
    CONDITIONAL,
    PASS,
    WARNING,
    evaluate_change,
)


def _full_signals(**overrides):
    base = {
        "inventory": {"atp": 500, "required": 480},
        "production": {"stage": "REL"},
        "transport": {"route_available": True, "carrier_capacity": True},
        "warehouse": {"pick_pack_feasible": True},
        "order_status": {"fulfillment_stage": 1},
        "sla": {"within_window": True, "days_to_deadline": 5},
        "dependencies": {"linked_orders": 0},
        "network": {"dc_routing_ok": True},
        "priority": {"customer_tier": "GOLD", "auto_approve": True},
    }
    base.update(overrides)
    return base


def _run(order_value_usd=5000.0, **kw):
    return evaluate_change(
        order_id="ORD-CHG-1",
        order_value_usd=order_value_usd,
        cosign_threshold_usd=HIGH_VALUE_OVERRIDE_THRESHOLD_USD,
        change_items=[{"field": "quantity", "from_value": "480", "to_value": "600"}],
        signals=_full_signals(**kw.pop("signals_override", {})),
        **kw,
    )


def test_all_signals_present_evaluates_ten_checks_and_validates_contract():
    out = _run()
    model = ChangeAnalysis(**out)  # round-trips into the typed section contract
    names = [c.name for c in model.evaluation.checks]
    # 9 signal-gated + the always-on Financial check.
    assert names == [
        "Inventory", "Production", "Transport", "Warehouse", "Order Status",
        "SLA", "Financial", "Dependencies", "Network", "Priority",
    ]
    assert model.evaluation.pass_count + model.evaluation.conditional_count == 10
    assert model.evaluation.lifecycle_stages[0] == "Created"
    assert model.evaluation.change_items[0].to_value == "600"


def test_variable_cardinality_only_signalled_constraints_evaluate():
    out = evaluate_change(
        order_id="o", order_value_usd=1000.0,
        cosign_threshold_usd=HIGH_VALUE_OVERRIDE_THRESHOLD_USD,
        signals={"inventory": {"atp": 10, "required": 10}},
    )
    names = [c["name"] for c in out["evaluation"]["checks"]]
    # Just Inventory (signalled) + the always-on Financial.
    assert names == ["Inventory", "Financial"]


def test_financial_always_runs_even_with_no_signals():
    out = evaluate_change(
        order_id="o", order_value_usd=1000.0,
        cosign_threshold_usd=HIGH_VALUE_OVERRIDE_THRESHOLD_USD, signals={})
    names = [c["name"] for c in out["evaluation"]["checks"]]
    assert names == ["Financial"]
    assert out["evaluation"]["checks"][0]["status"] == PASS


def test_cosign_gate_from_policy_threshold():
    below = _run(order_value_usd=HIGH_VALUE_OVERRIDE_THRESHOLD_USD - 1)
    assert below["decision"]["requires_cosign"] is False
    fin_below = next(c for c in below["evaluation"]["checks"] if c["name"] == "Financial")
    assert fin_below["status"] == PASS

    at = _run(order_value_usd=HIGH_VALUE_OVERRIDE_THRESHOLD_USD)
    assert at["decision"]["requires_cosign"] is True
    fin_at = next(c for c in at["evaluation"]["checks"] if c["name"] == "Financial")
    assert fin_at["status"] == CONDITIONAL


def test_clean_evaluation_recommends_approve():
    out = _run()
    assert out["evaluation"]["warning_count"] == 0
    approve = next(s for s in out["scenarios"] if s["name"] == "Approve as requested")
    assert approve["recommended"] is True
    assert out["decision"]["recommended_action"] == "Approve as requested"
    assert out["decision"]["confidence"] >= 0.9


def test_inventory_short_adds_partial_scenario_and_warning():
    out = _run(signals_override={"inventory": {"atp": 100, "required": 480}})
    inv = next(c for c in out["evaluation"]["checks"] if c["name"] == "Inventory")
    assert inv["status"] == WARNING
    names = [s["name"] for s in out["scenarios"]]
    assert "Partial fulfilment" in names
    assert "Reject / escalate to planner" in names
    # As-requested is no longer recommended once a warning is present.
    approve = next(s for s in out["scenarios"] if s["name"] == "Approve as requested")
    assert approve["recommended"] is False
    # Exactly one scenario is recommended.
    assert sum(1 for s in out["scenarios"] if s["recommended"]) == 1


def test_sla_risk_adds_expedite_scenario():
    out = _run(signals_override={"sla": {"within_window": True, "days_to_deadline": 1}})
    sla = next(c for c in out["evaluation"]["checks"] if c["name"] == "SLA")
    assert sla["status"] == CONDITIONAL
    assert "Expedite shipping" in [s["name"] for s in out["scenarios"]]


def test_confidence_drops_with_conditionals_and_warnings():
    clean = _run()
    warned = _run(signals_override={"inventory": {"atp": 100, "required": 480}})
    assert warned["decision"]["confidence"] < clean["decision"]["confidence"]


def test_deterministic_for_fixed_inputs():
    assert _run() == _run()


def test_lifecycle_index_passthrough():
    out = _run(lifecycle_index=2)
    assert out["evaluation"]["lifecycle_index"] == 2
