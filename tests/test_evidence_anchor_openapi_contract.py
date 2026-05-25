"""CP-B RED gate (ADR-043 §2.2) — EvidenceAnchor crosses the OpenAPI→TS seam.

Test-first (`xfail(strict=True)`; removed at CP-C after `scripts/export_openapi.py`
regenerates the schema and `asoe-ui` runs `generate-types`/`verify-types`). Mirrors
`test_inbox_gate_openapi_contract.py`: locks the cross-repo contract from the
backend side so the UI viewer's anchor type cannot silently drift. Spatial fields
are nullable here in Phase 1 and filled by ADR-045.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.xfail(
    reason="ADR-043 EvidenceAnchor schema exported at CP-C",
    strict=True,
)

_OPENAPI = Path(__file__).resolve().parent.parent / "openapi" / "asoe2.openapi.json"


def test_evidence_anchor_present_in_openapi_components():
    schemas = set(json.loads(_OPENAPI.read_text()).get("components", {}).get("schemas", {}))
    assert "EvidenceAnchor" in schemas
