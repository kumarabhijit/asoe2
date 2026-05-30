"""Generate the committed pipeline-topology snapshot from the live graph.

The compiled LangGraph (`orchestration/graph.py::get_pipeline_topology`)
is the source of truth for the Skill→Shadow→Recipe pipeline DAG the
operator's diagnostics view renders. The asoe-ui mock
(`MOCK_PIPELINE_TOPOLOGY` in `src/lib/api.ts`) hand-mirrors it so the
local/Vercel preview shows the same DAG as Azure. Nothing stopped the
two from silently desyncing — a backend graph reorder would ship a
correct DAG on Azure and a stale one on the preview, with no CI signal.

This script projects the live topology into committed JSON snapshots:

  - ``contracts/_generated/pipeline_topology.json``  (asoe2 golden)
  - ``<repo-parent>/asoe-ui/src/generated/pipeline_topology.json``
    (the UI parity copy — written only when the sibling asoe-ui repo
    is checked out alongside, mirroring the taxonomy generator)

Both sides assert against their snapshot:
  - asoe2 ``tests/test_pipeline_topology_drift.py`` — the live graph and
    the committed JSON match (graph changes must be regenerated).
  - asoe-ui ``tests/architectural/pipeline_topology_parity.test.ts`` —
    the mock matches the snapshot (mock can't drift from the graph).

Output is deterministic (sorted, identically formatted) so the drift
tests can compare bytes.

Usage:
    python -m scripts.generate_pipeline_topology            # write files
    python -m scripts.generate_pipeline_topology --check    # CI guard
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.seed_taxonomy import REPO_ROOT
from orchestration.graph import get_pipeline_topology

PRIMARY_OUT = REPO_ROOT / "contracts" / "_generated" / "pipeline_topology.json"
# The sibling asoe-ui repo (checked out next to asoe2 in dev/CI). Written
# only when present, exactly like the taxonomy generator's TS output.
UI_OUT = REPO_ROOT.parent / "asoe-ui" / "src" / "generated" / "pipeline_topology.json"


def _snapshot() -> str:
    """Deterministic JSON of the live topology: the canonical content the
    ``topology_hash`` is computed over, plus the hash itself."""
    topo = get_pipeline_topology()
    payload = {
        "topology_hash": topo.topology_hash,
        "nodes": [n.model_dump() for n in topo.nodes],
        "edges": [e.model_dump() for e in topo.edges],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _targets() -> list[Path]:
    targets = [PRIMARY_OUT]
    if UI_OUT.parent.is_dir():
        targets.append(UI_OUT)
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="Exit non-zero if any committed snapshot is out of date.",
    )
    args = parser.parse_args()

    text = _snapshot()
    if args.check:
        stale = []
        for target in _targets():
            if not target.exists() or target.read_text() != text:
                stale.append(str(target))
        if stale:
            print(
                "Pipeline-topology snapshot is out of date:\n  "
                + "\n  ".join(stale)
                + "\n\nRun: python -m scripts.generate_pipeline_topology",
                file=sys.stderr,
            )
            return 1
        return 0

    for target in _targets():
        target.write_text(text)
        print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
