"""Contract gate for the scenario analysis fixture.

``fixtures/scenarios/analyses.yaml`` is the per-record ``OrderAnalysis``
evidence payload the operator consumes (keyed by exc-id). asoe-ui vendors a
committed snapshot of this file and projects it into
``src/lib/mock-data/__generated__/scenario_analyses.ts``, which is spread
into the served ``MOCK_ORDER_ANALYSES``.

This test locks every entry to ``api/schemas.py::AnalysisResponse`` (the
same model the live read path returns from
``GET /api/v1/exceptions/{id}/analysis``). Because that model is
``extra="forbid"``, a fixture entry carrying a field the contract doesn't
declare — or omitting a required one — fails here, so the migrated UI
evidence cannot silently drift from the backend contract (Guardrail #6).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from api.schemas import AnalysisResponse

_ANALYSES_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "analyses.yaml"
)


def _load_analyses() -> dict:
    data = yaml.safe_load(_ANALYSES_PATH.read_text(encoding="utf-8")) or {}
    assert isinstance(data, dict), "analyses.yaml must parse to a mapping"
    assert "analyses" in data, "analyses.yaml must carry a top-level `analyses` key"
    analyses = data["analyses"] or {}
    assert isinstance(analyses, dict), "`analyses` must be a mapping keyed by exc-id"
    return analyses


def test_analyses_file_is_wellformed():
    data = yaml.safe_load(_ANALYSES_PATH.read_text(encoding="utf-8")) or {}
    assert data.get("version") == 1, "analyses.yaml must declare version: 1"
    assert _load_analyses(), "analyses.yaml carries no entries"


def _ids():
    # Collected at import time so each entry is a separately-reported case.
    return sorted(_load_analyses().keys())


@pytest.mark.parametrize("exc_id", _ids())
def test_entry_validates_against_analysis_response(exc_id: str):
    """Every analyses.yaml entry must satisfy the AnalysisResponse contract.

    ``AnalysisResponse`` is ``extra="forbid"``, so this catches both missing
    required fields and fields the contract doesn't declare.
    """
    entry = _load_analyses()[exc_id]
    assert isinstance(entry, dict), f"{exc_id}: entry must be a mapping"
    # Raises pydantic.ValidationError (failing the test) on any drift.
    AnalysisResponse.model_validate(entry)
