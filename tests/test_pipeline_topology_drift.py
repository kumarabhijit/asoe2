"""CI guard: the committed pipeline-topology snapshot tracks the live graph.

The compiled LangGraph is the source of truth for the pipeline DAG the
diagnostics view renders. `contracts/_generated/pipeline_topology.json`
is a committed projection of it, and the asoe-ui mock locks against a
copy of the same snapshot. If a developer reorders the graph and forgets
to regenerate, this fails — forcing a deliberate snapshot update (and,
via the asoe-ui parity test, a matching mock update) instead of a silent
preview-vs-Azure divergence.

Same test, both directions: hand-editing the JSON or changing the graph
without regenerating both trip it.
"""

from __future__ import annotations

from scripts.generate_pipeline_topology import PRIMARY_OUT, _snapshot


def test_committed_snapshot_matches_live_graph():
    expected = _snapshot()
    actual = PRIMARY_OUT.read_text()
    assert actual == expected, (
        "contracts/_generated/pipeline_topology.json is out of sync with "
        "the compiled graph (orchestration/graph.py).\n\n"
        "Run: python -m scripts.generate_pipeline_topology"
    )
