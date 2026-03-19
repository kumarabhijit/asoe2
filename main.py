from __future__ import annotations

# Entry point for manual smoke-testing.
#
# Uses run_graph() (not build_graph().invoke()) so that:
#   - Phase 6 kill switch (ASOE_KILL_SWITCH) is honoured
#   - Phase 6 explain mode (ASOE_EXPLAIN_MODE) is honoured
#   - Phase 5 observability trace is emitted
#   - The return value is a typed GraphState, not a raw dict

from contracts.models import GraphState, OrderEvent
from orchestration.graph import run_graph


def demo_event() -> OrderEvent:
    return OrderEvent(
        order_id="SO-1001",
        line_item=1,
        po_price=90.0,
        sap_base_price=100.0,
        retailer_id="R-01",
        line_count=1,
    )


if __name__ == "__main__":
    state = GraphState(event=demo_event())
    result = run_graph(state)
    print(result)
