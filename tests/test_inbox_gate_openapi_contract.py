"""Phase-0 RED gate — OpenAPI carries the new Customer-Inbox section schemas.

The UI types are generated from `openapi/asoe2.openapi.json`. The ADR-042
evidence sections must exist as Pydantic models surfaced into the OpenAPI
components before the asoe-ui `generate-types` / `verify-types` round-trip can
land. This locks the cross-repo contract from the backend side.

Written TEST-FIRST as xfail(strict). **Marker removed in ADR-042 Phase 7**: all
8 documented section schemas (ADR-042 §2.2.1) are now exported — the last two
(`KnowledgeGraphPayload`, `DraftReply`) landed with the Knowledge Graph tab and
the Draft-Reply analysis projection. This is now a standing hard gate: removing
any section schema fails CI.
"""

from __future__ import annotations

import json
from pathlib import Path

_OPENAPI = Path(__file__).resolve().parent.parent / "openapi" / "asoe2.openapi.json"

# Names declared in ADR-042 §2.2.1 (api/schemas.py section models).
_REQUIRED_SCHEMAS = {
    "OrderEntryExtraction",
    "Edi850Document",
    "ConstraintEvaluation",
    "ConstraintCheck",
    "ScenarioOption",
    "ChangeDecision",
    "KnowledgeGraphPayload",
    "DraftReply",
}


def _schema_names() -> set[str]:
    doc = json.loads(_OPENAPI.read_text())
    return set(doc.get("components", {}).get("schemas", {}))


def test_openapi_file_exists() -> None:
    assert _OPENAPI.is_file()


def test_section_schemas_present() -> None:
    missing = _REQUIRED_SCHEMAS - _schema_names()
    assert not missing, f"missing OpenAPI section schemas: {sorted(missing)}"
