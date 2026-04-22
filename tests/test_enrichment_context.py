"""Pillar 1 tests — GraphState / ExceptionRecord / _persist_exception
carry enrichment_context from graph execution to the audit trail.

Invariants:
  * New GraphState.enrichment_context field defaults to {} and is
    dict-typed.
  * New ExceptionRecord.enrichment_context attribute is initialised
    (empty dict on records that didn't populate it).
  * `_persist_exception` captures state.enrichment_context when
    populated explicitly.
  * When state.enrichment_context is empty, the bridge falls back
    to state.resolved_data so pre-migration gateway code continues
    to contribute evidence. This keeps the change non-breaking.
  * Persisted context is a copy — mutating the graph state after
    persistence must not alter the record.
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


class TestPersistExceptionBridge:
    """The bridge in api/routes/exceptions.py::_persist_exception:
    prefer state.enrichment_context, fall back to state.resolved_data,
    otherwise empty. Proves the migration is non-breaking."""

    def test_explicit_enrichment_context_persists_verbatim(self):
        from api.routes.exceptions import _persist_exception

        exception_store.clear()
        state = GraphState(event=_minimal_event())
        state.enrichment_context = {"primary_dc": {"plant": "DC-1"}}
        state.resolved_data = {"legacy_key": "ignored_when_explicit"}
        exc_id = _persist_exception("test-tenant", state, trace_id="tr-1")
        record = exception_store.get(exc_id, "test-tenant")
        assert record.enrichment_context == {"primary_dc": {"plant": "DC-1"}}

    def test_resolved_data_fallback_when_context_empty(self):
        """Pre-migration gateways still write to resolved_data. The
        bridge captures it so their evidence isn't lost."""
        from api.routes.exceptions import _persist_exception

        exception_store.clear()
        state = GraphState(event=_minimal_event())
        state.resolved_data = {"matched_po_details": {"po": "SO-9"}}
        # state.enrichment_context left at its default empty dict.
        exc_id = _persist_exception("test-tenant", state, trace_id="tr-2")
        record = exception_store.get(exc_id, "test-tenant")
        assert record.enrichment_context == {"matched_po_details": {"po": "SO-9"}}

    def test_empty_when_both_sources_empty(self):
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
