"""Pure-function unit tests for the Knowledge Graph builder (ADR-042 Phase 7).

`gateways/knowledge_graph.build_knowledge_graph` derives a node/edge graph from a
case's already-resolved entities (no standalone KG source — ADR §5b). These
tests lock the node/edge derivation, dedup, the empty/minimal case, determinism,
and the round-trip into `KnowledgeGraphPayload`. No I/O, no LLM.
"""

from __future__ import annotations

from api.schemas import KnowledgeGraphPayload
from gateways.knowledge_graph import build_knowledge_graph


def _full(**overrides):
    base = dict(
        order_id="0093847612",
        customer_name="Walmart Stores Inc",
        customer_bp="300001",
        line_items=[
            {"material": "BEV-COLA-12PK", "description": "Cola 12-pack", "quantity": 480, "uom": "CS"},
            {"material": "BEV-LEMON-6PK", "quantity": 120, "uom": "CS"},
        ],
        sap={"system": "S4H_PRD", "sap_doc_number": "5100012344", "validation_status": "SO confirmed, ATP OK"},
        entities=[{"key": "ship_to", "value": "DC-6094", "kind": "location"}],
    )
    base.update(overrides)
    return base


def test_full_projection_nodes_edges_and_root():
    g = build_knowledge_graph(**_full())
    model = KnowledgeGraphPayload(**g)  # round-trips into the contract
    kinds = {n.kind for n in model.nodes}
    assert kinds == {"order", "customer", "material", "sap_doc", "entity"}
    assert model.root_id == "order:0093847612"
    # Customer places the order; order contains materials; validated_by SAP doc.
    relations = {(e.source, e.relation) for e in model.edges}
    assert ("customer:300001", "places") in relations
    assert (model.root_id, "contains") in {(e.source, e.relation) for e in model.edges}
    assert any(e.relation == "validated_by" for e in model.edges)
    assert any(e.relation == "mentions" for e in model.edges)
    # Two materials → two contains edges.
    assert sum(1 for e in model.edges if e.relation == "contains") == 2


def test_minimal_order_only_has_single_root_node():
    g = build_knowledge_graph(order_id="SO-1")
    assert g["root_id"] == "order:so-1"
    assert len(g["nodes"]) == 1
    assert g["nodes"][0]["kind"] == "order"
    assert g["edges"] == []


def test_empty_lines_and_entities_are_skipped():
    g = build_knowledge_graph(
        order_id="SO-1",
        line_items=[{"material": ""}, {"description": "no material"}],
        entities=[{"key": "k", "value": None}],
    )
    assert [n["kind"] for n in g["nodes"]] == ["order"]


def test_duplicate_entity_value_does_not_double_add():
    # An entity whose value duplicates a material is still namespaced by kind,
    # so it is a distinct node; a repeated identical entity is deduped.
    g = build_knowledge_graph(
        order_id="SO-1",
        entities=[
            {"key": "po", "value": "X", "kind": "po"},
            {"key": "po", "value": "X", "kind": "po"},
        ],
    )
    entity_nodes = [n for n in g["nodes"] if n["kind"] == "entity"]
    assert len(entity_nodes) == 1


def test_deterministic_for_fixed_inputs():
    assert build_knowledge_graph(**_full()) == build_knowledge_graph(**_full())
