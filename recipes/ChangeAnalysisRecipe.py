from __future__ import annotations

# Change Analysis Recipe (ADR-042 Phase 6 — the Order Change Workflow).
#
# Deterministic evaluation of a requested order change against the prototype's
# constraint catalogue (Inventory, Production, Transport, Warehouse, Order
# Status, SLA, Financial, Dependencies, Network, Priority), the resolution
# scenarios it implies, and a decision. This is the recipe-homed replacement
# for the prototype's client-side "7-agent" evaluator.
#
# Architecture (ADR-042 §6, Architect correction):
#   * Constraint evaluation is DETERMINISTIC RECIPE LOGIC — it lives here, NOT
#     in `constraints/` (that package is the constrained-LLM-generation router)
#     and NOT in `agents/harness.py` (a gated-off single-case loop).
#   * Thresholds are INJECTED by the caller (the orchestration / producer layer
#     reads `contracts/policy.py` and passes `cosign_threshold_usd`). The recipe
#     never imports policy directly (Invariant #11 — recipes stay policy-free;
#     thresholds arrive as validated params, like every other recipe). No
#     invented thresholds (Guardrail #1).
#   * VARIABLE CARDINALITY: only constraints whose backing signal is present are
#     evaluated, so the result is an N-length list (not the prototype's fixed
#     10). The Financial check always runs (it reads the order value directly).
#   * Pure function: no I/O, no LLM, no clock, no randomness. Same inputs →
#     identical output, so the evaluation is auditable and unit-testable.
#   * Recipes return dicts; the composer projects them into the typed contract
#     (`api.schemas.ChangeAnalysis`) — no "ready-to-render" assembly here
#     (Guardrail #6).

from typing import Any, Callable, Dict, List, Optional

# Status vocabulary (matches the prototype constraint card states).
PASS = "PASS"
CONDITIONAL = "CONDITIONAL"
WARNING = "WARNING"

# Sales-order lifecycle stage bar (Created → … → Shipped). Surfaced in the
# payload so the UI renders the bar from the backend, not a hardcoded list.
LIFECYCLE_STAGES: List[str] = [
    "Created", "Confirmed", "Released", "Picked", "Shipped",
]


def evaluate_change(
    *,
    order_id: str,
    order_value_usd: float,
    cosign_threshold_usd: float,
    change_items: Optional[List[Dict[str, Any]]] = None,
    signals: Optional[Dict[str, Any]] = None,
    lifecycle_index: Optional[int] = None,
) -> Dict[str, Any]:
    """Evaluate a requested order change. See the module docstring for the
    contract. Returns the `api.schemas.ChangeAnalysis` shape as a plain dict.

    Args:
      order_id: host trace / order identifier.
      order_value_usd: the order's value — drives the Financial check + the
        cosign gate.
      cosign_threshold_usd: the four-eyes materiality threshold, injected by the
        caller from `contracts/policy.HIGH_VALUE_OVERRIDE_THRESHOLD_USD` (the
        recipe stays policy-free — Invariant #11).
      change_items: the requested changes ``[{field, from_value, to_value}]``.
      signals: per-constraint backing data; a constraint is evaluated only when
        its key is present (variable cardinality). Recognised keys: inventory,
        production, transport, warehouse, order_status, sla, dependencies,
        network, priority.
      lifecycle_index: current position within ``LIFECYCLE_STAGES``.
    """
    signals = signals or {}
    items = [
        {
            "field": str(ci.get("field", "")),
            "from_value": _opt_str(ci.get("from_value")),
            "to_value": _opt_str(ci.get("to_value")),
        }
        for ci in (change_items or [])
        if ci.get("field")
    ]

    checks = _evaluate_constraints(signals, order_value_usd, cosign_threshold_usd)
    counts = _tally(checks)
    scenarios = _generate_scenarios(checks, order_value_usd)
    decision = _decide(checks, counts, scenarios, order_value_usd,
                       cosign_threshold_usd)

    return {
        "evaluation": {
            "lifecycle_stages": list(LIFECYCLE_STAGES),
            "lifecycle_index": lifecycle_index,
            "change_items": items,
            "checks": checks,
            "pass_count": counts[PASS],
            "conditional_count": counts[CONDITIONAL],
            "warning_count": counts[WARNING],
        },
        "scenarios": scenarios,
        "decision": decision,
    }


# ---------------------------------------------------------------------------
# constraint evaluators (pure) — each returns a check dict or None when its
# backing signal is absent.
# ---------------------------------------------------------------------------

def _check(name: str, status: str, detail: str, *, metric: Optional[str] = None,
           agent: Optional[str] = None,
           system_ref: Optional[str] = None) -> Dict[str, Any]:
    return {
        "name": name, "status": status, "detail": detail,
        "metric": metric, "agent": agent, "system_ref": system_ref,
    }


def _inventory(sig: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    s = sig.get("inventory")
    if not isinstance(s, dict):
        return None
    atp = _as_float(s.get("atp"))
    required = _as_float(s.get("required"))
    if atp is None or required is None:
        return None
    if atp >= required:
        status, detail = PASS, "ATP covers the requested quantity."
    elif atp >= 0.8 * required:
        status, detail = CONDITIONAL, "ATP partially covers the change; partial ship feasible."
    else:
        status, detail = WARNING, "ATP is short of the requested quantity."
    return _check("Inventory", status, detail,
                  metric=f"ATP {_num(atp)} vs {_num(required)} required",
                  agent="Inventory Agent", system_ref="SAP MM/ATP")


def _production(sig: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    s = sig.get("production")
    if not isinstance(s, dict):
        return None
    stage = str(s.get("stage", "")).upper()
    mapping = {
        "REL": (PASS, "Production order released; change can be incorporated."),
        "CRTD": (CONDITIONAL, "Production order created but not firm; review capacity."),
        "TECO": (WARNING, "Production technically complete; change is disruptive."),
    }
    if stage not in mapping:
        return None
    status, detail = mapping[stage]
    return _check("Production", status, detail, metric=f"Order status {stage}",
                  agent="Production Agent", system_ref="SAP PP")


def _transport(sig: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    s = sig.get("transport")
    if not isinstance(s, dict):
        return None
    route = bool(s.get("route_available"))
    capacity = bool(s.get("carrier_capacity"))
    if route and capacity:
        status, detail = PASS, "Route and carrier capacity available."
    elif route or capacity:
        status, detail = CONDITIONAL, "Partial transport availability; re-plan may be needed."
    else:
        status, detail = WARNING, "No route or carrier capacity for the change."
    return _check("Transport", status, detail,
                  metric=f"route={route}, carrier={capacity}",
                  agent="Transport Agent", system_ref="TMS")


def _warehouse(sig: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    s = sig.get("warehouse")
    if not isinstance(s, dict):
        return None
    feasible = bool(s.get("pick_pack_feasible"))
    status = PASS if feasible else WARNING
    detail = ("Pick/pack is feasible for the change."
              if feasible else "Pick/pack is not feasible at the current stage.")
    return _check("Warehouse", status, detail,
                  metric=f"pick_pack_feasible={feasible}",
                  agent="Warehouse Agent", system_ref="WMS")


def _order_status(sig: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    s = sig.get("order_status")
    if not isinstance(s, dict):
        return None
    stage = _as_int(s.get("fulfillment_stage"))
    if stage is None:
        return None
    if stage <= 1:
        status, detail = PASS, "Order is early in fulfilment; change is low-risk."
    elif stage <= 3:
        status, detail = CONDITIONAL, "Order is mid-fulfilment; change needs coordination."
    else:
        status, detail = WARNING, "Order is late in fulfilment; change is high-risk."
    return _check("Order Status", status, detail,
                  metric=f"fulfilment stage {stage}/5",
                  agent="Order Lifecycle Agent", system_ref="SAP SD")


def _sla(sig: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    s = sig.get("sla")
    if not isinstance(s, dict):
        return None
    within = bool(s.get("within_window"))
    days = _as_int(s.get("days_to_deadline"))
    if not within:
        status, detail = WARNING, "Change breaches the contractual delivery window."
    elif days is not None and days < 2:
        status, detail = CONDITIONAL, "Change is within window but the deadline is tight."
    else:
        status, detail = PASS, "Change stays within the contractual delivery window."
    metric = f"{days} days to deadline" if days is not None else "within window"
    return _check("SLA", status, detail, metric=metric,
                  agent="SLA Agent", system_ref="Contract DB")


def _financial(order_value_usd: float, threshold_usd: float) -> Dict[str, Any]:
    # Always evaluated — reads the order value directly (no signal gate).
    if order_value_usd >= threshold_usd:
        status = CONDITIONAL
        detail = "Revenue impact meets the four-eyes threshold; cosign required."
    else:
        status = PASS
        detail = "Revenue impact is below the four-eyes threshold."
    return _check("Financial", status, detail,
                  metric=f"{_usd(order_value_usd)} revenue impact",
                  agent="Finance Agent", system_ref="SAP FI/CO")


def _dependencies(sig: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    s = sig.get("dependencies")
    if not isinstance(s, dict):
        return None
    linked = _as_int(s.get("linked_orders"))
    if linked is None:
        return None
    if linked == 0:
        status, detail = PASS, "No linked orders or deliveries affected."
    elif linked <= 2:
        status, detail = CONDITIONAL, "A few linked orders may need re-coordination."
    else:
        status, detail = WARNING, "Many linked orders/deliveries are affected."
    return _check("Dependencies", status, detail,
                  metric=f"{linked} linked orders",
                  agent="Dependency Agent", system_ref="SAP SD")


def _network(sig: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    s = sig.get("network")
    if not isinstance(s, dict):
        return None
    ok = bool(s.get("dc_routing_ok"))
    status = PASS if ok else CONDITIONAL
    detail = ("DC routing supports the change."
              if ok else "DC routing needs re-optimisation for the change.")
    return _check("Network", status, detail, metric=f"dc_routing_ok={ok}",
                  agent="Network Optimization", system_ref="Network Opt")


def _priority(sig: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    s = sig.get("priority")
    if not isinstance(s, dict):
        return None
    tier = _opt_str(s.get("customer_tier"))
    auto = bool(s.get("auto_approve"))
    if auto:
        status, detail = PASS, "Customer tier qualifies the change for auto-approval."
    else:
        status, detail = CONDITIONAL, "Customer tier requires manual approval of the change."
    return _check("Priority", status, detail,
                  metric=f"tier {tier}" if tier else None,
                  agent="Priority Agent", system_ref="CRM")


# Ordered catalogue — stable output ordering, variable membership.
_SIGNAL_EVALUATORS: List[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = [
    _inventory, _production, _transport, _warehouse, _order_status,
    _sla, _dependencies, _network, _priority,
]


def _evaluate_constraints(signals: Dict[str, Any], order_value_usd: float,
                          cosign_threshold_usd: float) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    for evaluator in _SIGNAL_EVALUATORS:
        check = evaluator(signals)
        if check is not None:
            checks.append(check)
        # Financial sits at its catalogue position (#7) but always runs.
        if evaluator is _sla:
            checks.append(_financial(order_value_usd, cosign_threshold_usd))
    return checks


def _tally(checks: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {PASS: 0, CONDITIONAL: 0, WARNING: 0}
    for c in checks:
        if c["status"] in counts:
            counts[c["status"]] += 1
    return counts


# ---------------------------------------------------------------------------
# scenarios + decision (pure, deterministic)
# ---------------------------------------------------------------------------

def _has(checks: List[Dict[str, Any]], name: str, statuses: set) -> bool:
    return any(c["name"] == name and c["status"] in statuses for c in checks)


def _generate_scenarios(checks: List[Dict[str, Any]],
                        order_value_usd: float) -> List[Dict[str, Any]]:
    counts = _tally(checks)
    scenarios: List[Dict[str, Any]] = []

    # Always offer the as-requested change.
    approve_ok = counts[WARNING] == 0
    scenarios.append({
        "name": "Approve as requested",
        "description": "Apply the requested change to the order as-is.",
        "recommended": approve_ok,
        "impact": ("All constraints clear or conditional."
                   if approve_ok else "One or more constraints flag a warning."),
        "financial_delta_usd": 0.0,
    })

    # Partial fulfilment when supply/logistics constraints are not clean.
    if (_has(checks, "Inventory", {CONDITIONAL, WARNING})
            or _has(checks, "Transport", {CONDITIONAL, WARNING})
            or _has(checks, "Warehouse", {CONDITIONAL, WARNING})):
        scenarios.append({
            "name": "Partial fulfilment",
            "description": "Fulfil the available quantity now and backorder the remainder.",
            "recommended": not approve_ok,
            "impact": "Protects the delivery date for the available portion.",
            "financial_delta_usd": -round(order_value_usd * 0.25, 2),
        })

    # Expedite when the SLA is at risk.
    if _has(checks, "SLA", {CONDITIONAL, WARNING}):
        scenarios.append({
            "name": "Expedite shipping",
            "description": "Upgrade the carrier service to hold the SLA window.",
            "recommended": False,
            "impact": "Holds the delivery window at added freight cost.",
            "financial_delta_usd": -round(order_value_usd * 0.05, 2),
        })

    # Reject / escalate when something hard-fails.
    if counts[WARNING] > 0:
        recommended = not any(s["recommended"] for s in scenarios)
        scenarios.append({
            "name": "Reject / escalate to planner",
            "description": "Decline the change and route to a supply planner.",
            "recommended": recommended,
            "impact": "Avoids committing to an infeasible change.",
            "financial_delta_usd": None,
        })

    return scenarios


def _decide(checks: List[Dict[str, Any]], counts: Dict[str, int],
            scenarios: List[Dict[str, Any]],
            order_value_usd: float,
            cosign_threshold_usd: float) -> Dict[str, Any]:
    recommended = next((s for s in scenarios if s["recommended"]), None)
    action = recommended["name"] if recommended else "Manual review required"

    # Deterministic confidence from the constraint mix.
    confidence = 1.0 - 0.1 * counts[CONDITIONAL] - 0.25 * counts[WARNING]
    confidence = max(0.3, min(0.98, round(confidence, 2)))

    if counts[WARNING] > 0:
        rationale = "One or more constraints raised a warning; see the flagged checks."
    elif counts[CONDITIONAL] > 0:
        rationale = "Change is feasible with conditions; review the conditional checks."
    else:
        rationale = "All evaluated constraints pass; change is low-risk."

    requires_cosign = order_value_usd >= cosign_threshold_usd

    return {
        "recommended_action": action,
        "confidence": confidence,
        "rationale": rationale,
        "revenue_impact_usd": round(order_value_usd, 2),
        "requires_cosign": requires_cosign,
        "sap_actions": _sap_actions(checks),
    }


def _sap_actions(checks: List[Dict[str, Any]]) -> List[str]:
    # Deterministic SAP action hints from the evaluated constraints.
    actions = ["VA02: update sales order"]
    if _has(checks, "Production", {CONDITIONAL, WARNING}):
        actions.append("CO02: review production order")
    if _has(checks, "Transport", {CONDITIONAL, WARNING}):
        actions.append("VT02N: re-plan shipment")
    return actions


# ---------------------------------------------------------------------------
# coercion helpers (pure)
# ---------------------------------------------------------------------------

def _opt_str(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    f = _as_float(value)
    return None if f is None else int(f)


def _num(value: float) -> str:
    return str(int(value)) if value == int(value) else ("%g" % value)


def _usd(value: float) -> str:
    return "${:,.2f}".format(value)
