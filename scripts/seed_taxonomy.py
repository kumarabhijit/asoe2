"""Idempotent loader: ``db/seeds/case_taxonomy.yaml`` -> taxonomy DB tables.

Used by:
  - ``db/migrations/V017__case_taxonomy.sql`` (Postgres path: SQL seeds inline).
  - ``db/migrations/runner.py::_apply_sqlite_v017`` (SQLite path: imports
    ``load_taxonomy()``).
  - Steward CLI workflow (``scripts/steward_change.py``, Phase 6).
  - The drift-check test (``tests/test_taxonomy_constants_drift.py``).

The loader is **idempotent**: re-running it against an already-seeded DB
must produce no net change (UPSERT semantics, no duplicate rows).

Requirements: docs/specs/case-intent-supergroup-requirements.md §6, §7.
Plan: docs/plans/case-intent-supergroup-implementation-plan.md §3.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

try:
    import yaml  # PyYAML, declared in pyproject dependencies
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "PyYAML is required to load the taxonomy seed; install via uv sync"
    ) from exc

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = REPO_ROOT / "db" / "seeds" / "case_taxonomy.yaml"
SCHEMA_PATH = REPO_ROOT / "db" / "seeds" / "case_taxonomy.schema.json"


# ---------------------------------------------------------------------------
# Pure loaders / validators
# ---------------------------------------------------------------------------

def load_yaml(path: Path = SEED_PATH) -> dict[str, Any]:
    """Parse and JSON-schema-validate the seed YAML.

    Validation is mandatory — a malformed seed cannot reach the DB or the
    generated constants. Raises ValueError with a precise location on
    failure.
    """
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    _validate(data)
    _validate_invariants(data)
    return data


def _validate(data: Any) -> None:
    """Validate against JSON schema. ``jsonschema`` is a hard requirement
    (declared in pyproject) so a malformed seed can never reach the DB
    silently on a misconfigured host."""
    import jsonschema  # hard dep per pyproject.toml

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ValueError(f"taxonomy seed validation failed: {exc.message}") from exc


def _validate_invariants(data: dict[str, Any]) -> None:
    """Structural invariants the JSON schema cannot express.

    1. Every intent's ``supergroup_code`` must reference a known supergroup.
    2. Every label's ``code`` must reference a known SG or INT, matching
       the declared ``domain``.
    3. Code uniqueness within supergroups and intents.
    4. The lint rule from requirements §9.2: no shared suffix between
       SG_ and INT_ (e.g. SG_DUPLICATE_PO + INT_DUPLICATE_PO is banned).
    """
    sg_codes = {row["code"] for row in data["supergroups"]}
    int_codes = {row["code"] for row in data["intents"]}

    if len(sg_codes) != len(data["supergroups"]):
        raise ValueError("duplicate supergroup code in seed")
    if len(int_codes) != len(data["intents"]):
        raise ValueError("duplicate intent code in seed")

    for intent in data["intents"]:
        if intent["supergroup_code"] not in sg_codes:
            raise ValueError(
                f"intent {intent['code']!r} references unknown "
                f"supergroup {intent['supergroup_code']!r}"
            )

    for label in data["labels"]:
        if label["domain"] == "SUPERGROUP":
            if label["code"] not in sg_codes:
                raise ValueError(
                    f"label {label['code']!r} (SUPERGROUP) has no matching supergroup"
                )
        else:
            if label["code"] not in int_codes:
                raise ValueError(
                    f"label {label['code']!r} (INTENT) has no matching intent"
                )

    # Requirements §9.2: no shared suffix between SG_ and INT_ namespaces.
    sg_suffixes = {c.removeprefix("SG_") for c in sg_codes}
    int_suffixes = {c.removeprefix("INT_") for c in int_codes}
    collisions = sg_suffixes & int_suffixes
    if collisions:
        raise ValueError(
            "naming collision: shared suffix between SG_ and INT_ "
            f"namespaces: {sorted(collisions)}"
        )


# ---------------------------------------------------------------------------
# SQLite writer (used by runner.py V017 path)
# ---------------------------------------------------------------------------

def load_taxonomy_sqlite(conn: sqlite3.Connection, data: dict[str, Any] | None = None) -> None:
    """Write the parsed seed into a SQLite DB. Idempotent.

    Assumes the four taxonomy tables already exist (created by V017 DDL).
    """
    if data is None:
        data = load_yaml()
    today = date.today().isoformat()

    for sg in data["supergroups"]:
        conn.execute(
            """
            INSERT INTO case_supergroup
                (code, origin, description, owner_role, is_active,
                 effective_from, deprecated_at, replaced_by_code, sort_order, version)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, 1)
            ON CONFLICT(code) DO UPDATE SET
                origin = excluded.origin,
                description = excluded.description,
                owner_role = excluded.owner_role,
                sort_order = excluded.sort_order,
                deprecated_at = excluded.deprecated_at,
                replaced_by_code = excluded.replaced_by_code,
                version = case_supergroup.version + 1
            """,
            (
                sg["code"], sg["origin"], sg["description"], sg["owner_role"],
                today, sg.get("deprecated_at"), sg.get("replaced_by_code"),
                sg["sort_order"],
            ),
        )

    for intent in data["intents"]:
        conn.execute(
            """
            INSERT INTO case_intent
                (code, supergroup_code, description, sap_block_code, sap_block_field,
                 sap_sales_org, is_active, effective_from, deprecated_at, replaced_by_code)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                supergroup_code = excluded.supergroup_code,
                description = excluded.description,
                sap_block_code = excluded.sap_block_code,
                sap_block_field = excluded.sap_block_field,
                sap_sales_org = excluded.sap_sales_org,
                deprecated_at = excluded.deprecated_at,
                replaced_by_code = excluded.replaced_by_code
            """,
            (
                intent["code"], intent["supergroup_code"], intent["description"],
                intent.get("sap_block_code"), intent.get("sap_block_field"),
                intent.get("sap_sales_org"), today,
                intent.get("deprecated_at"), intent.get("replaced_by_code"),
            ),
        )
        # Mirror the (supergroup_code, intent_code) allowed pair. v1 is 1:1
        # but the table supports M:N for future evolution.
        conn.execute(
            """
            INSERT INTO supergroup_intent_allowed
                (supergroup_code, intent_code, effective_from, deprecated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(supergroup_code, intent_code) DO UPDATE SET
                deprecated_at = excluded.deprecated_at
            """,
            (
                intent["supergroup_code"], intent["code"], today,
                intent.get("deprecated_at"),
            ),
        )

    for label in data["labels"]:
        conn.execute(
            """
            INSERT INTO intent_label
                (code, domain, locale, display_name, description)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(code, domain, locale) DO UPDATE SET
                display_name = excluded.display_name,
                description = excluded.description
            """,
            (
                label["code"], label["domain"], label.get("locale", "en"),
                label["display_name"], label.get("description"),
            ),
        )

    conn.commit()


__all__ = [
    "SEED_PATH",
    "SCHEMA_PATH",
    "load_yaml",
    "load_taxonomy_sqlite",
]
