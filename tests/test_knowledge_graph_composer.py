"""ADR-042 Phase 7 — KnowledgeGraphPayload composer + AnalysisResponse wiring.

Composer projects the derived graph from enrichment_context; None when absent,
empty, or malformed (preview-only / deferrable, Guardrail #6). Build logic is
locked in tests/test_knowledge_graph_builder.py.
"""

from __future__ import annotations

from api.profile_composer import compose_knowledge_graph
from api.schemas import AnalysisResponse, KnowledgeGraphPayload
from api.store import ExceptionRecord
from gateways.knowledge_graph import build_knowledge_graph


def _record(**overrides) -> ExceptionRecord:
    base = dict(
        tenant_id="acme-corp", order_id="SO-1", event_type="MANUAL_ORDER_INTAKE",
        trace_id="tr-1", intent="MANUAL_ORDER_INTAKE",
        lifecycle_state="PENDING_REVIEW", shadow_verdict="YELLOW",
        resolution_data={},
    )
    base.update(overrides)
    return ExceptionRecord(**base)


_CTX = build_knowledge_graph(
    order_id="SO-1", customer_name="Walmart Stores Inc", customer_bp="300001",
    line_items=[{"material": "BEV-COLA-12PK", "quantity": 480, "uom": "CS"}],
)


def test_compose_none_when_context_absent() -> None:
    assert compose_knowledge_graph(_record()) is None


def test_compose_none_when_nodes_empty() -> None:
    assert compose_knowledge_graph(
        _record(enrichment_context={"knowledge_graph": {"nodes": [], "edges": []}})
    ) is None


def test_compose_projects_from_context() -> None:
    out = compose_knowledge_graph(
        _record(enrichment_context={"knowledge_graph": _CTX})
    )
    assert out is not None
    assert out.root_id == "order:so-1"
    assert any(n.kind == "customer" for n in out.nodes)


def test_analysis_response_carries_knowledge_graph() -> None:
    ar = AnalysisResponse(
        diagnosis="d", confidence=90, risk="low", resolution="r",
        knowledge_graph=KnowledgeGraphPayload(**_CTX),
    )
    assert ar.knowledge_graph is not None
    assert AnalysisResponse(
        diagnosis="d", confidence=90, risk="low", resolution="r",
    ).knowledge_graph is None
