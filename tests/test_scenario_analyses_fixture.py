"""Validation lock for the scenario ANALYSIS fixture.

`fixtures/scenarios/analyses.yaml` is the asoe2-owned, exc-id-keyed
source of truth for the per-record `OrderAnalysis` evidence payload the
operator consumes. It is a sibling to `catalog.yaml` (which carries the
SAP-seed detection inputs under `scenarios:`); these two files share no
keyspace.

asoe-ui vendors a committed snapshot of this file and projects it into
`src/lib/mock-data/__generated__/scenario_analyses.ts`, which the mock
API client consumes for migrated intent families (slice 1:
PRICE_HOLD_RELEASE). Because the UI renders these payloads on a
financially-binding, SOX-relevant surface (Guardrail #6), every entry
must validate against the same Pydantic contract the real `/analysis`
endpoint emits — `api.schemas.AnalysisResponse` and its `*AnalysisData`
sub-models (which set `extra="forbid"`, so a stray field is a hard
failure here rather than a silent partial-truth state in the UI).

Bug shape this catches: a fixture entry that drifts from the wire
contract (renamed field, wrong enum literal, extra key) landing without
the UI mock layer noticing — exactly the drift the test-strategy
fixture/validation requirement exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from api.schemas import AnalysisResponse

ANALYSES_PATH = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "scenarios"
    / "analyses.yaml"
)


def _load_analyses() -> dict[str, dict]:
    data = yaml.safe_load(ANALYSES_PATH.read_text())
    assert isinstance(data, dict), "analyses.yaml must be a mapping"
    analyses = data.get("analyses")
    assert isinstance(analyses, dict), "analyses.yaml must carry an `analyses` mapping"
    return analyses


def test_analyses_fixture_exists_and_is_non_empty():
    analyses = _load_analyses()
    assert analyses, "analyses.yaml carries no entries"


def test_every_entry_is_keyed_by_exc_id():
    for key in _load_analyses():
        assert key.startswith("exc-"), f"unexpected analysis key {key!r} (expected exc-*)"


@pytest.mark.parametrize("exc_id", sorted(_load_analyses().keys()))
def test_entry_validates_against_analysis_response(exc_id: str):
    """Each entry round-trips through the AnalysisResponse contract.

    `extra="forbid"` on the `*AnalysisData` sub-models means a stray or
    misspelled enrichment field fails here, mirroring the UI's dumb-projector
    contract (Guardrail #6) rather than surfacing a partial-truth state.
    """
    entry = _load_analyses()[exc_id]
    try:
        model = AnalysisResponse.model_validate(entry)
    except ValidationError as exc:  # pragma: no cover - failure path
        pytest.fail(f"{exc_id} does not validate against AnalysisResponse:\n{exc}")

    # The fixture is the recipe/composer projection — it must NOT carry the
    # runtime-only presentation hint (the UI's read path stamps it).
    assert "primary_section" not in entry, (
        f"{exc_id} must not carry primary_section — it is a runtime presentation "
        f"hint stamped by the read path, not recipe/composer output."
    )
    assert model.diagnosis, f"{exc_id} missing diagnosis"


def test_price_hold_release_slice_present_and_shaped():
    """Slice-1 shape lock: PRICE_HOLD_RELEASE records carry their evidence."""
    analyses = _load_analyses()
    auto = AnalysisResponse.model_validate(analyses["exc-017"])
    escalate = AnalysisResponse.model_validate(analyses["exc-018"])

    assert auto.price_hold_analysis is not None
    assert auto.price_hold_analysis.action == "AUTO_RELEASE"
    assert auto.price_hold_analysis.hold_status == "RELEASED"

    assert escalate.price_hold_analysis is not None
    assert escalate.price_hold_analysis.action == "ESCALATE"
    assert escalate.price_hold_analysis.hold_status == "HELD"
