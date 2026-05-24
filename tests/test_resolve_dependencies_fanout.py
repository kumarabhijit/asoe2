"""Regression: the dependency fan-out must not share GatewayExecutor._pool.

`resolve_dependencies` fans out N gateway calls by submitting to a pool, and
each `GatewayExecutor.run` *recursively* submits the actual `gateway.execute`
to enforce its per-call timeout. If both use the SAME pool, the outer tasks can
saturate it so the inner `execute` submits never get a worker — a classic
recursive-submit self-deadlock once a recipe's dependency count approaches the
pool size (and worse across concurrent requests, since the pool is process-wide).
The in-code fix the GatewayExecutor docstring calls for: give the outer fan-out
its own pool.

This test forces the pathological interleaving deterministically: N gateway
calls that each block on a shared Barrier until *all N* are simultaneously
in flight. With a shared pool the inner calls can't all run, the barrier never
trips, and every dependency comes back empty. With decoupled pools all N inner
calls run, the barrier trips, and every dependency populates.

Written test-first; RED on the shared-pool parent, GREEN once the fan-out pool
is decoupled.
"""

from __future__ import annotations

import dataclasses
import threading

from contracts.models import GatewayDependency, GatewayRequest, GatewayResponse, GraphState, OrderEvent
from gateways.registry import clear_registry, register_gateway
from orchestration.nodes import resolve_dependencies
from recipes import registry as registry_mod

_N = 12  # > the recursive ceiling of a single shared 16-worker pool (2N > 16)


class _BarrierGateway:
    """Returns SUCCESS only once all N concurrent execute() calls have met at
    the barrier — i.e. only when the inner calls truly run in parallel."""

    def __init__(self, name: str, barrier: threading.Barrier) -> None:
        self._name = name
        self._barrier = barrier

    @property
    def name(self) -> str:
        return self._name

    def execute(self, request: GatewayRequest) -> GatewayResponse:
        try:
            self._barrier.wait(timeout=2.0)
        except threading.BrokenBarrierError:
            return GatewayResponse(
                gateway_name=self._name, operation=request.operation,
                status="FAILED", error="barrier not reached (fan-out starved)",
            )
        return GatewayResponse(
            gateway_name=self._name, operation=request.operation,
            status="SUCCESS", data={"ok": True},
        )

    def health_check(self) -> bool:
        return True


def test_dependency_fanout_does_not_self_deadlock(monkeypatch) -> None:
    clear_registry()
    barrier = threading.Barrier(_N)
    register_gateway(_BarrierGateway("barrier_gw", barrier))

    temp_name = "TempFanoutProbeRecipe.py"
    base_spec = registry_mod.REGISTRY["ManualOrderIntakeRecipe.py"]
    probe_spec = dataclasses.replace(
        base_spec,
        name=temp_name,
        dependencies=tuple(
            GatewayDependency(
                gateway_name="barrier_gw", operation="op",
                params_from_state={}, result_key=f"k{i}", required_for_audit=False,
            )
            for i in range(_N)
        ),
    )
    monkeypatch.setitem(registry_mod.REGISTRY, temp_name, probe_spec)

    state = GraphState(event=OrderEvent(
        order_id="o-1", event_type="MANUAL_ORDER_INTAKE",
        po_price=0.0, sap_base_price=0.0,
    ))
    state.selected_recipe = temp_name
    state.request_trace_id = "trace-fanout"

    out = resolve_dependencies(state)

    # Every dependency must have resolved to the gateway's SUCCESS payload.
    # Under the shared-pool deadlock these come back empty (FAILED soft-fail).
    for i in range(_N):
        assert out.enrichment_context.get(f"k{i}") == {"ok": True}, (
            f"dependency k{i} did not resolve — fan-out starved the inner pool"
        )

    clear_registry()
