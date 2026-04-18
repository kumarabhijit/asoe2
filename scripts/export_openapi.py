"""Export the asoe2 FastAPI OpenAPI schema to openapi/asoe2.openapi.json.

Phase 2 #9 — drift-proof the frontend/backend contract. The frontend
(asoe-ui) consumes this file via openapi-typescript to generate its
TypeScript request/response types; a CI job in each repo regenerates
the artifact and fails if the checked-in copy is stale.

Usage:
    python scripts/export_openapi.py

Output:
    openapi/asoe2.openapi.json (formatted, stable ordering)
"""

from __future__ import annotations

import json
from pathlib import Path

from api.app import create_app


def export() -> None:
    app = create_app()
    schema = app.openapi()
    # Strip the server-local hash so the artifact is stable across runs.
    schema.pop("servers", None)
    out = Path(__file__).resolve().parent.parent / "openapi" / "asoe2.openapi.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out.relative_to(Path.cwd())}")


if __name__ == "__main__":
    export()
