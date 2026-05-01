"""ADR-027 Phase A — pipeline topology endpoint + introspection helper.

The compiled-graph topology is the source of truth; the UI must consume
it from `/api/v1/pipeline/topology` rather than mirroring node names. The
fitness tests here are the architectural lock that asserts compiled-graph
parity (Phase A acceptance criterion).
"""
from __future__ import annotations

import pytest

pytest.importorskip("langgraph", reason="langgraph not installed")

from fastapi.testclient import TestClient  # noqa: E402

from api.app import create_app  # noqa: E402
from api.deps import create_test_token  # noqa: E402
from api.schemas import PipelineTopology  # noqa: E402
from orchestration.graph import (  # noqa: E402
    _LANGGRAPH_SYNTHETIC_NODES,
    build_graph,
    get_pipeline_topology,
    get_verdict_labels,
)


@pytest.fixture()
def client():
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {create_test_token()}"}


# ---------------------------------------------------------------------------
# Introspection helper — pure projection of compiled graph + A.0 registry
# ---------------------------------------------------------------------------


class TestPipelineTopologyHelper:
    def test_returns_pipeline_topology_model(self):
        topology = get_pipeline_topology()
        assert isinstance(topology, PipelineTopology)
        assert topology.topology_hash
        assert len(topology.topology_hash) == 64  # sha256 hex

    def test_nodes_match_compiled_graph_minus_synthetics(self):
        topology = get_pipeline_topology()
        compiled_nodes = set(build_graph().get_graph().nodes.keys())
        expected = compiled_nodes - _LANGGRAPH_SYNTHETIC_NODES
        assert {n.id for n in topology.nodes} == expected

    def test_build_analysis_marked_as_terminal(self):
        topology = get_pipeline_topology()
        kinds = {n.id: n.kind for n in topology.nodes}
        assert kinds["build_analysis"] == "terminal"
        for node in topology.nodes:
            if node.id != "build_analysis":
                assert node.kind == "node", node

    def test_no_synthetic_nodes_or_edges_leak_through(self):
        topology = get_pipeline_topology()
        for n in topology.nodes:
            assert n.id not in _LANGGRAPH_SYNTHETIC_NODES
        for e in topology.edges:
            assert e.from_node not in _LANGGRAPH_SYNTHETIC_NODES
            assert e.to_node not in _LANGGRAPH_SYNTHETIC_NODES

    def test_topology_hash_is_stable_across_calls(self):
        a = get_pipeline_topology()
        b = get_pipeline_topology()
        assert a.topology_hash == b.topology_hash

    def test_every_conditional_edge_has_a_verdict_label(self):
        topology = get_pipeline_topology()
        for edge in topology.edges:
            if edge.conditional:
                assert edge.verdict_label, (
                    f"{edge.from_node} -> {edge.to_node} is conditional but "
                    f"has no verdict_label"
                )

    def test_unconditional_edges_have_no_verdict_label(self):
        topology = get_pipeline_topology()
        for edge in topology.edges:
            if not edge.conditional:
                assert edge.verdict_label is None

    def test_shadow_audit_terminal_edge_expands_to_red_and_yellow(self):
        topology = get_pipeline_topology()
        shadow_terminals = [
            e for e in topology.edges
            if e.from_node == "shadow_audit" and e.to_node == "build_analysis"
        ]
        verdicts = sorted(e.verdict_label for e in shadow_terminals)
        assert verdicts == ["red", "yellow"]

    def test_shadow_audit_continue_edge_emits_green(self):
        topology = get_pipeline_topology()
        green = [
            e for e in topology.edges
            if e.from_node == "shadow_audit"
            and e.to_node == "execute_recipe"
            and e.verdict_label == "green"
        ]
        assert len(green) == 1

    def test_implicit_classify_disagreement_edge_present(self):
        topology = get_pipeline_topology()
        implicit = [
            e for e in topology.edges
            if e.from_node == "classify"
            and e.to_node == "build_analysis"
            and e.verdict_label == "cross_check_disagreement"
        ]
        assert len(implicit) == 1
        assert implicit[0].conditional is True

    def test_implicit_continue_does_not_duplicate_unconditional_edge(self):
        topology = get_pipeline_topology()
        classify_to_load_skill = [
            e for e in topology.edges
            if e.from_node == "classify" and e.to_node == "load_skill"
        ]
        assert len(classify_to_load_skill) == 1
        assert classify_to_load_skill[0].conditional is False

    def test_every_compiled_conditional_edge_is_represented(self):
        compiled = build_graph().get_graph()
        topology = get_pipeline_topology()
        compiled_pairs = {
            (e.source, e.target)
            for e in compiled.edges
            if e.conditional
            and e.source not in _LANGGRAPH_SYNTHETIC_NODES
            and e.target not in _LANGGRAPH_SYNTHETIC_NODES
        }
        topology_pairs = {
            (e.from_node, e.to_node)
            for e in topology.edges
            if e.conditional and e.from_node in get_verdict_labels()
        }
        assert compiled_pairs == topology_pairs

    def test_topology_hash_changes_when_topology_changes(self, monkeypatch):
        original = get_pipeline_topology()
        from orchestration import graph as graph_module
        original_labels = dict(graph_module._VERDICT_LABELS)
        try:
            tweaked = dict(original_labels)
            tweaked.pop("validate_types")
            monkeypatch.setattr(graph_module, "_VERDICT_LABELS", tweaked)
            mutated = get_pipeline_topology()
            assert mutated.topology_hash != original.topology_hash
        finally:
            monkeypatch.setattr(graph_module, "_VERDICT_LABELS", original_labels)


# ---------------------------------------------------------------------------
# Endpoint — GET /api/v1/pipeline/topology
# ---------------------------------------------------------------------------


class TestPipelineTopologyEndpoint:
    def test_unauthenticated_request_is_rejected(self, client):
        r = client.get("/api/v1/pipeline/topology")
        assert r.status_code == 401

    def test_invalid_token_is_rejected(self, client):
        r = client.get(
            "/api/v1/pipeline/topology",
            headers={"Authorization": "Bearer notajwt"},
        )
        assert r.status_code == 401

    def test_authenticated_request_returns_topology(self, client, auth_header):
        r = client.get("/api/v1/pipeline/topology", headers=auth_header)
        assert r.status_code == 200
        body = r.json()
        assert "topology_hash" in body
        assert "nodes" in body
        assert "edges" in body

    def test_endpoint_matches_helper_output(self, client, auth_header):
        helper = get_pipeline_topology()
        r = client.get("/api/v1/pipeline/topology", headers=auth_header)
        assert r.status_code == 200
        assert r.json() == helper.model_dump()

    def test_endpoint_does_not_require_role(self, client):
        # ADR-027 §"Backend changes" #2: authentication only — no
        # additional permission. A `viewer` token must succeed.
        token = create_test_token(roles=["viewer"])
        r = client.get(
            "/api/v1/pipeline/topology",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200

    def test_partner_role_can_read_topology(self, client):
        # Topology is the graph SHAPE — same nodes/edges for everyone —
        # so a partner-role token (which is otherwise tightly scoped on
        # per-record data) succeeds here.
        token = create_test_token(roles=["partner"])
        r = client.get(
            "/api/v1/pipeline/topology",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Architectural lock — every conditional edge in the compiled graph has a
# verdict label, every registered downstream is a real graph node.
# ---------------------------------------------------------------------------


class TestArchitecturalLock:
    def test_every_compiled_conditional_edge_has_a_verdict_label(self):
        topology = get_pipeline_topology()
        compiled = build_graph().get_graph()
        for compiled_edge in compiled.edges:
            if not compiled_edge.conditional:
                continue
            if compiled_edge.source in _LANGGRAPH_SYNTHETIC_NODES:
                continue
            if compiled_edge.target in _LANGGRAPH_SYNTHETIC_NODES:
                continue
            matches = [
                e for e in topology.edges
                if e.from_node == compiled_edge.source
                and e.to_node == compiled_edge.target
                and e.conditional
            ]
            assert matches, (
                f"compiled-graph conditional edge "
                f"{compiled_edge.source}->{compiled_edge.target} has no "
                f"verdict label in PipelineTopology"
            )
            assert all(m.verdict_label for m in matches)

    def test_no_orphan_verdict_labels_in_registry(self):
        compiled = build_graph().get_graph()
        compiled_pairs = {
            (e.source, e.target)
            for e in compiled.edges
            if e.conditional
        }
        for gate, labels in get_verdict_labels().items():
            for verdict_key, downstream in labels.items():
                assert (gate, downstream) in compiled_pairs, (
                    f"_VERDICT_LABELS[{gate!r}][{verdict_key!r}] points at "
                    f"{downstream!r} which is not a conditional-edge target "
                    f"of {gate!r} in the compiled graph"
                )
