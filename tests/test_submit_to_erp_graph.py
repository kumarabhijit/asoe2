"""ADR-042 Phase 3 — the ERP submit executes on the deterministic graph path.

The operator-authorised order-entry submit re-enters the LangGraph with a
*directed* SubmitToErpRecipe: classify (intent stays MANUAL_ORDER_INTAKE) →
select_recipe (honours directed_recipe) → validate_types (builds the submit
invocation from the reviewed extraction + operator corrections) → Compliance
Shadow → execute_recipe (build the ERP payload) → apply_effects (the `erp`
gateway write). This keeps the financial write on the deterministic path
(CLAUDE.md Guardrail #1), not inline in an API handler.

Deterministic (no live model — the conftest registers the stub gateways and the
default backend is the deterministic fallback). Written test-first.
"""

from __future__ import annotations

from contracts.models import GraphState, OrderEvent, TerminalStatus
from orchestration.graph import run_graph

_ORDER_CTX = {
    "order_entry_extraction": {
        "source_type": "PDF",
        "confidence": 0.94,
        "header": {
            "customer_po": "0093847612", "order_type": "ZOR",
            "sales_org": "1000", "dist_channel": "10",
            "requested_date": "2025-03-17",
        },
        "customer_name": "Walmart Stores Inc",
        "customer_bp": "300001",
        "line_items": [
            {"line_num": "001", "material": "BEV-COLA-12PK", "quantity": 480,
             "uom": "CS", "unit_price": 8.64},
        ],
        "validation_flags": [],
    }
}


def _submit_state(**overrides) -> GraphState:
    base = dict(
        event=OrderEvent(
            order_id="SO-SUBMIT-1", event_type="MANUAL_ORDER_INTAKE",
            po_price=0.0, sap_base_price=0.0,
        ),
        enrichment_context=dict(_ORDER_CTX),
        directed_recipe="SubmitToErpRecipe.py",
    )
    base.update(overrides)
    return GraphState(**base)


def test_directed_submit_runs_on_graph_and_writes_erp() -> None:
    out = run_graph(_submit_state())
    assert out.selected_recipe == "SubmitToErpRecipe.py"
    assert out.intent.value == "MANUAL_ORDER_INTAKE"
    assert out.execution_log is not None
    assert out.execution_log.outputs["status"] == "SUCCESS"
    assert out.execution_log.outputs["erp_payload"]["customer_po"] == "0093847612"
    # The operator-authorised submit reaches COMPLETE — the DoR #2 auto-execute
    # guard exempts the submit recipe (it gates the *classifier*).
    assert out.final_status == TerminalStatus.COMPLETE
    # The ERP write effect fired on the deterministic path.
    assert any(
        r.gateway_name == "erp" and r.status == "SUCCESS"
        for r in out.effect_results
    )


def test_directed_submit_applies_operator_corrections() -> None:
    out = run_graph(_submit_state(
        directed_corrections={"lines": {"001": {"quantity": 500}}},
    ))
    line = out.execution_log.outputs["erp_payload"]["line_items"][0]
    assert line["quantity"] == 500
    assert {"line_num": "001", "field": "quantity", "before": 480, "after": 500} \
        in out.execution_log.outputs["corrections_applied"]


def test_rejected_submit_does_not_fire_erp_write() -> None:
    # An order that can't be submitted (no line items) must NOT call the ERP
    # write — effects only fire on a COMPLETE recipe.
    ctx = {"order_entry_extraction": {
        **_ORDER_CTX["order_entry_extraction"], "line_items": [],
    }}
    out = run_graph(_submit_state(enrichment_context=ctx))
    assert out.execution_log.outputs["status"] == "REJECTED"
    assert out.final_status == TerminalStatus.REJECTED
    assert not any(r.gateway_name == "erp" for r in out.effect_results)
