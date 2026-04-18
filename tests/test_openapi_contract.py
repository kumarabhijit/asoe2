"""Phase 2 #9 — drift-detect test for the committed OpenAPI schema.

Regenerates the schema from the live app and diffs against the
checked-in openapi/asoe2.openapi.json. Any drift means the contract
has changed without the artifact being refreshed — regenerate with::

    python scripts/export_openapi.py
"""

from __future__ import annotations

import json
from pathlib import Path

from api.app import create_app


_REPO_ROOT = Path(__file__).resolve().parent.parent
_ARTIFACT = _REPO_ROOT / "openapi" / "asoe2.openapi.json"


def test_openapi_artifact_up_to_date() -> None:
    live = create_app().openapi()
    live.pop("servers", None)
    expected = json.dumps(live, indent=2, sort_keys=True) + "\n"
    actual = _ARTIFACT.read_text(encoding="utf-8")
    assert actual == expected, (
        "openapi/asoe2.openapi.json is stale. Regenerate with:\n"
        "    python scripts/export_openapi.py"
    )
