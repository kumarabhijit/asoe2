"""Contract gate for the scenario line-item fixture.

``fixtures/scenarios/line_items.yaml`` is the line-level evidence table the
operator sees in the EvidenceGrid (the ``/exceptions/{id}/line-items``
surface), keyed by exc-id. asoe-ui vendors a committed snapshot of this file
and projects it into
``src/lib/mock-data/__generated__/scenario_line_items.ts``, which the served
``exceptionsApi.lineItems()`` returns in mock mode.

This test locks every row to ``api/schemas.py::LineItem`` (the same model the
live read path returns). Because that model is the response contract, a
fixture row carrying a field the contract doesn't declare — or omitting a
required one — fails here, so the migrated UI line-item evidence cannot
silently drift from the backend contract.

It also locks the fixture's *coverage* against the catalog: every keyed
record must be a real catalog scenario id, so the line-item table can't drift
to records the queue never surfaces.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from api.schemas import LineItem

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios"
_LINE_ITEMS_PATH = _FIXTURES / "line_items.yaml"
_CATALOG_PATH = _FIXTURES / "catalog.yaml"


def _load_line_items() -> dict:
    data = yaml.safe_load(_LINE_ITEMS_PATH.read_text(encoding="utf-8")) or {}
    assert isinstance(data, dict), "line_items.yaml must parse to a mapping"
    assert "line_items" in data, "line_items.yaml must carry a top-level `line_items` key"
    rows = data["line_items"] or {}
    assert isinstance(rows, dict), "`line_items` must be a mapping keyed by exc-id"
    return rows


def _catalog_ids() -> set[str]:
    catalog = yaml.safe_load(_CATALOG_PATH.read_text(encoding="utf-8")) or {}
    ids: set[str] = set()
    for key in ("scenarios", "email_scenarios"):
        for entry in catalog.get(key) or []:
            if entry.get("id"):
                ids.add(str(entry["id"]))
    return ids


def test_line_items_file_is_wellformed():
    data = yaml.safe_load(_LINE_ITEMS_PATH.read_text(encoding="utf-8")) or {}
    assert data.get("version") == 1, "line_items.yaml must declare version: 1"
    assert _load_line_items(), "line_items.yaml carries no entries"


def test_every_record_is_a_catalog_scenario():
    """Coverage lock — the line-item table cannot drift to records the
    catalog (and therefore the queue) never surfaces."""
    catalog_ids = _catalog_ids()
    orphans = sorted(k for k in _load_line_items() if k not in catalog_ids)
    assert orphans == [], f"line_items.yaml records not in the catalog: {orphans}"


def _ids():
    return sorted(_load_line_items().keys())


@pytest.mark.parametrize("exc_id", _ids())
def test_rows_validate_against_line_item(exc_id: str):
    """Every row must satisfy the ``LineItem`` response contract, and the
    join key (``line_id``) must be unique within a record so the EvidenceGrid
    can join per-line analysis (``OrderAnalysis.lines``) deterministically."""
    rows = _load_line_items()[exc_id]
    assert isinstance(rows, list) and rows, f"{exc_id}: line items must be a non-empty list"
    line_ids = []
    for row in rows:
        model = LineItem(**row)  # raises on missing/extra/mistyped fields
        line_ids.append(model.line_id)
    assert len(line_ids) == len(set(line_ids)), f"{exc_id}: duplicate line_id in {line_ids}"
