"""Pillar 1 tests — GraphState / ExceptionRecord / _persist_exception
carry enrichment_context from graph execution to the audit trail.

Invariants (post-V004 single-bag semantics):
  * GraphState.enrichment_context defaults to {} and is dict-typed.
  * ExceptionRecord.enrichment_context defaults to {} when omitted.
  * `_persist_exception` captures state.enrichment_context verbatim;
    there is no fallback to state.resolved_data — gateway results
    land in enrichment_context directly via resolve_dependencies.
  * Persisted context is a copy — mutating the graph state after
    persistence must not alter the record.
  * resolve_dependencies writes ONLY to enrichment_context; the
    legacy resolved_data dual-write was retired with V004.
"""

from __future__ import annotations

from contracts.models import GraphState, OrderEvent
from api.store import ExceptionRecord, exception_store


def _minimal_event() -> OrderEvent:
    return OrderEvent(
        order_id="ORD-ENRICH-1",
        po_price=100.0,
        sap_base_price=100.0,
        event_type="GENERIC",
    )


class TestGraphStateEnrichmentContext:
    def test_defaults_to_empty_dict(self):
        state = GraphState(event=_minimal_event())
        assert state.enrichment_context == {}
        assert isinstance(state.enrichment_context, dict)

    def test_accepts_arbitrary_payload(self):
        payload = {
            "matched_po_details": {"po_number": "SO-1", "total": 420.0},
            "primary_dc": {"plant": "DC-1", "qty": 50},
        }
        state = GraphState(event=_minimal_event(), enrichment_context=payload)
        assert state.enrichment_context == payload

    def test_is_distinct_from_resolved_data(self):
        """The two dicts have different lifetimes — resolved_data is
        recipe input (transient), enrichment_context is audit-persisted.
        They must not share identity."""
        state = GraphState(event=_minimal_event())
        state.resolved_data["foo"] = "bar"
        assert "foo" not in state.enrichment_context


class TestExceptionRecordEnrichmentContext:
    def test_defaults_to_empty_dict(self):
        record = ExceptionRecord(
            tenant_id="t1", order_id="ORD-1",
            event_type="GENERIC", trace_id="trace-1",
        )
        assert record.enrichment_context == {}

    def test_captures_payload_verbatim(self):
        payload = {"contract_ref": "CNT-42", "warehouse": {"plant": "DC-2"}}
        record = ExceptionRecord(
            tenant_id="t1", order_id="ORD-1",
            event_type="GENERIC", trace_id="trace-1",
            enrichment_context=payload,
        )
        assert record.enrichment_context == payload

    def test_empty_when_omitted(self):
        record = ExceptionRecord(
            tenant_id="t1", order_id="ORD-1",
            event_type="GENERIC", trace_id="trace-1",
            enrichment_context=None,
        )
        assert record.enrichment_context == {}


class TestPersistExceptionSingleBag:
    """`_persist_exception` reads gateway evidence ONLY from
    state.enrichment_context. The pre-V004 `resolved_data → enrichment_context`
    fallback was retired — gateway nodes write to enrichment_context
    directly (see resolve_dependencies)."""

    def test_explicit_enrichment_context_persists_verbatim(self):
        from api.routes.exceptions import _persist_exception

        exception_store.clear()
        state = GraphState(event=_minimal_event())
        state.enrichment_context = {"primary_dc": {"plant": "DC-1"}}
        # Anything left in resolved_data is ignored — recipe-input bag,
        # not audit evidence.
        state.resolved_data = {"legacy_key": "ignored"}
        exc_id = _persist_exception("test-tenant", state, trace_id="tr-1")
        record = exception_store.get(exc_id, "test-tenant")
        assert record.enrichment_context == {"primary_dc": {"plant": "DC-1"}}

    def test_resolved_data_is_not_bridged_into_enrichment_context(self):
        """V004 single-bag: gateway evidence flows via enrichment_context
        only. Anything written to resolved_data is recipe-transient and
        does not survive to the audit record."""
        from api.routes.exceptions import _persist_exception

        exception_store.clear()
        state = GraphState(event=_minimal_event())
        state.resolved_data = {"matched_po_details": {"po": "SO-9"}}
        # state.enrichment_context left at its default empty dict.
        exc_id = _persist_exception("test-tenant", state, trace_id="tr-2")
        record = exception_store.get(exc_id, "test-tenant")
        assert record.enrichment_context == {}

    def test_empty_when_enrichment_context_unset(self):
        from api.routes.exceptions import _persist_exception

        exception_store.clear()
        state = GraphState(event=_minimal_event())
        exc_id = _persist_exception("test-tenant", state, trace_id="tr-3")
        record = exception_store.get(exc_id, "test-tenant")
        assert record.enrichment_context == {}

    def test_persisted_context_is_decoupled_from_state(self):
        """Mutating the graph state after persistence must not alter
        the record. `_persist_exception` copies."""
        from api.routes.exceptions import _persist_exception

        exception_store.clear()
        state = GraphState(event=_minimal_event())
        state.enrichment_context = {"key": "original"}
        exc_id = _persist_exception("test-tenant", state, trace_id="tr-4")
        state.enrichment_context["key"] = "mutated"
        record = exception_store.get(exc_id, "test-tenant")
        assert record.enrichment_context == {"key": "original"}


class TestResolveDependenciesSingleBag:
    """resolve_dependencies writes gateway results ONLY to
    state.enrichment_context. resolved_data is no longer touched by
    the gateway resolution path (V004 single-bag)."""

    def _stub_spec_with_one_dep(self, result_key="thing"):
        from types import SimpleNamespace
        from contracts.models import GatewayDependency

        return SimpleNamespace(
            dependencies=(
                GatewayDependency(
                    gateway_name="oms",
                    operation="get_thing",
                    params_from_state={"order_id": "event.order_id"},
                    result_key=result_key,
                ),
            ),
        )

    def test_gateway_result_lands_in_enrichment_context_only(self, monkeypatch):
        from contracts.models import GatewayResponse, GraphState
        from orchestration import nodes
        from orchestration.nodes import resolve_dependencies

        spec = self._stub_spec_with_one_dep()
        monkeypatch.setattr(nodes, "get_recipe", lambda _name: spec)

        class _StubExecutor:
            def __init__(self):
                import concurrent.futures
                self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

            def run(self, request):
                return GatewayResponse(
                    gateway_name=request.gateway_name,
                    operation=request.operation,
                    status="SUCCESS",
                    data={"po": "SO-42"},
                )

        monkeypatch.setattr(nodes, "GatewayExecutor", _StubExecutor)

        state = GraphState(event=_minimal_event())
        state.selected_recipe = "TestRecipe.py"
        result = resolve_dependencies(state)
        assert result.enrichment_context == {"thing": {"po": "SO-42"}}
        assert result.resolved_data == {}

    def test_no_dependencies_is_noop(self):
        from contracts.models import GraphState
        from orchestration.nodes import resolve_dependencies

        state = GraphState(event=_minimal_event())
        # No selected_recipe → early return; both bags untouched.
        result = resolve_dependencies(state)
        assert result.enrichment_context == {}
        assert result.resolved_data == {}
