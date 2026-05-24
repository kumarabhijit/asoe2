from __future__ import annotations

# Knowledge Graph builder (ADR-042 Phase 7 — the Knowledge Graph tab).
#
# A pure, deterministic projection of a case's already-resolved entities into a
# node/edge graph: the order at the centre, its customer, each ordered material,
# the SAP document, and any AI-extracted entities. There is NO standalone
# knowledge-graph data source today (ADR-042 §5b / §5c — the `knowledge/`
# package is the skill/policy KB, not a graph producer), so this DERIVES the
# graph from existing enrichment context rather than inventing relationships.
# Deferrable behind real demand; when the case carries no projectable entities
# the builder returns an empty graph and the composer omits the tab.
#
# Pure function: no I/O, no LLM, no clock, no randomness — node ids are stable
# slugs of the entity identity, so the same case always yields the same graph
# (auditable + unit-testable). Returns the `api.schemas.KnowledgeGraphPayload`
# shape as a plain dict; the composer projects it (Guardrail #6).

import re
from typing import Any, Dict, List, Optional


def build_knowledge_graph(
    *,
    order_id: str,
    customer_name: Optional[str] = None,
    customer_bp: Optional[str] = None,
    line_items: Optional[List[Dict[str, Any]]] = None,
    sap: Optional[Dict[str, Any]] = None,
    entities: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Project a case's entities into a knowledge graph.

    Args:
      order_id: the order/case identifier — becomes the focal (root) node.
      customer_name / customer_bp: the buying customer (a `customer` node).
      line_items: ordered lines ``[{material, description, quantity, uom}]`` →
        one `material` node each, linked ``order --contains--> material``.
      sap: the SAP validation read ``{system, sap_doc_number, validation_status}``
        → a `sap_doc` node linked ``order --validated_by--> sap_doc``.
      entities: AI-extracted entities ``[{key, value, kind}]`` → `entity` nodes
        linked ``order --mentions--> entity`` (deduped against existing nodes).

    Returns the `api.schemas.KnowledgeGraphPayload` shape. Pure + deterministic.
    """
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    seen: set = set()

    def add_node(node_id: str, label: str, kind: str,
                 detail: Optional[str] = None) -> Optional[str]:
        if not node_id or node_id in seen:
            return node_id if node_id in seen else None
        seen.add(node_id)
        nodes.append({"id": node_id, "label": label, "kind": kind,
                      "detail": detail})
        return node_id

    # Focal node — the order.
    root_id = f"order:{_slug(order_id)}"
    add_node(root_id, order_id, "order", detail="Sales order")

    # Customer.
    if customer_name or customer_bp:
        cust_id = f"customer:{_slug(customer_bp or customer_name)}"
        if add_node(cust_id, customer_name or customer_bp, "customer",
                    detail=f"BP {customer_bp}" if customer_bp else None):
            edges.append({"source": cust_id, "target": root_id,
                          "relation": "places"})

    # Materials.
    for li in line_items or []:
        material = li.get("material")
        if not material:
            continue
        mat_id = f"material:{_slug(str(material))}"
        qty = li.get("quantity")
        uom = li.get("uom")
        detail = li.get("description")
        if detail is None and qty is not None:
            detail = f"{_num(qty)} {uom}".strip() if uom else _num(qty)
        if add_node(mat_id, str(material), "material", detail=detail):
            edges.append({"source": root_id, "target": mat_id,
                          "relation": "contains"})

    # SAP document.
    if isinstance(sap, dict):
        doc = sap.get("sap_doc_number")
        system = sap.get("system")
        if doc or system:
            sap_id = f"sap_doc:{_slug(str(doc or system))}"
            if add_node(sap_id, str(doc or system), "sap_doc",
                        detail=sap.get("validation_status") or system):
                edges.append({"source": root_id, "target": sap_id,
                              "relation": "validated_by"})

    # AI-extracted entities (skip ones already represented as typed nodes).
    for ent in entities or []:
        value = ent.get("value")
        if not value:
            continue
        ent_id = f"entity:{_slug(str(ent.get('kind') or 'entity'))}:{_slug(str(value))}"
        if ent_id in seen:
            continue
        kind = ent.get("kind") or "entity"
        if add_node(ent_id, str(value), "entity", detail=str(kind)):
            edges.append({"source": root_id, "target": ent_id,
                          "relation": "mentions"})

    return {"nodes": nodes, "edges": edges, "root_id": root_id}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")


def _num(value: Any) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f == int(f) else ("%g" % f)
