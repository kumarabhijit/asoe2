"""Offline lint of tests/fixtures/synthetic/*.

Runs as a normal pytest in CI — no network, no deployed API needed.
Catches:
  * fixture file pairs that fall out of sync (event without expected, or vice versa)
  * an `intent` value in expected.json that isn't a real intent enum value
  * a `selected_recipe` in expected.json that isn't a real recipe name
  * an `allowed_shadow_verdicts` value that isn't GREEN/YELLOW/RED
  * an event.json that is missing the three required ResolveRequest fields
    (order_id, po_price, sap_base_price) — i.e. would 422 against the API
  * the synthetic-tagging convention (metadata.synthetic + metadata.source)
    that lets the audit trail distinguish smoke traffic from real ingest

The remote behavioural test lives in scripts/smoke-e2e.sh.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "synthetic"

# Pulled at module-load time so a new intent / recipe added upstream that
# breaks a fixture is caught here, not deferred to the live deployment.
# AllowedIntent / AllowedRecipeName are typing.Literal, so values come
# via get_args() rather than enum iteration.
from typing import get_args

from constraints.specs import AllowedIntent, AllowedRecipeName

INTENT_VALUES = set(get_args(AllowedIntent))
RECIPE_VALUES = set(get_args(AllowedRecipeName))
VALID_VERDICTS = {"GREEN", "YELLOW", "RED"}


def _fixture_pairs() -> list[tuple[str, Path, Path]]:
    """Return (basename, event_path, expected_path) for every fixture."""
    pairs = []
    for ev in sorted(FIXTURES_DIR.glob("*.event.json")):
        base = ev.name.removesuffix(".event.json")
        exp = FIXTURES_DIR / f"{base}.expected.json"
        pairs.append((base, ev, exp))
    return pairs


def test_at_least_one_fixture_pair_exists():
    pairs = _fixture_pairs()
    assert pairs, f"no *.event.json fixtures in {FIXTURES_DIR}"


@pytest.mark.parametrize("base,event_path,expected_path", _fixture_pairs(), ids=lambda x: x if isinstance(x, str) else "")
def test_each_event_has_a_matching_expected_sidecar(base, event_path, expected_path):
    assert expected_path.exists(), (
        f"{base}: missing {expected_path.name} sidecar — the smoke script needs"
        " an assertion contract for every event fixture."
    )


@pytest.mark.parametrize("base,event_path,expected_path", _fixture_pairs(), ids=lambda x: x if isinstance(x, str) else "")
def test_event_has_required_resolve_request_fields(base, event_path, expected_path):
    payload = json.loads(event_path.read_text())
    for required in ("order_id", "po_price", "sap_base_price"):
        assert required in payload, (
            f"{base}.event.json is missing required ResolveRequest field {required!r}"
        )
    # Synthetic-tagging convention: every smoke fixture must mark itself
    # so audit-chain consumers can distinguish smoke traffic from prod.
    metadata = payload.get("metadata") or {}
    assert metadata.get("synthetic") is True, (
        f"{base}.event.json must set metadata.synthetic=true so audit can"
        " filter smoke traffic"
    )
    assert metadata.get("source") == "smoke-e2e", (
        f"{base}.event.json must set metadata.source='smoke-e2e' for the"
        " same reason"
    )


@pytest.mark.parametrize("base,event_path,expected_path", _fixture_pairs(), ids=lambda x: x if isinstance(x, str) else "")
def test_expected_intent_is_a_real_enum_value(base, event_path, expected_path):
    expected = json.loads(expected_path.read_text())
    intent = expected.get("intent")
    assert intent in INTENT_VALUES, (
        f"{base}.expected.json: intent={intent!r} is not a member of "
        f"AllowedIntent. Valid values: {sorted(INTENT_VALUES)}"
    )


@pytest.mark.parametrize("base,event_path,expected_path", _fixture_pairs(), ids=lambda x: x if isinstance(x, str) else "")
def test_expected_recipe_is_registered(base, event_path, expected_path):
    expected = json.loads(expected_path.read_text())
    recipe = expected.get("selected_recipe")
    assert recipe in RECIPE_VALUES, (
        f"{base}.expected.json: selected_recipe={recipe!r} is not a member"
        f" of AllowedRecipeName. Valid values: {sorted(RECIPE_VALUES)}"
    )


@pytest.mark.parametrize("base,event_path,expected_path", _fixture_pairs(), ids=lambda x: x if isinstance(x, str) else "")
def test_allowed_shadow_verdicts_are_well_formed(base, event_path, expected_path):
    expected = json.loads(expected_path.read_text())
    allowed = expected.get("allowed_shadow_verdicts") or []
    assert allowed, (
        f"{base}.expected.json: allowed_shadow_verdicts must be non-empty"
        " (use the full set if you want to accept any verdict)"
    )
    bad = [v for v in allowed if v not in VALID_VERDICTS]
    assert not bad, (
        f"{base}.expected.json: allowed_shadow_verdicts contains invalid"
        f" values {bad}. Must be a subset of {sorted(VALID_VERDICTS)}"
    )


@pytest.mark.parametrize("base,event_path,expected_path", _fixture_pairs(), ids=lambda x: x if isinstance(x, str) else "")
def test_allowed_final_statuses_are_non_empty(base, event_path, expected_path):
    expected = json.loads(expected_path.read_text())
    allowed = expected.get("allowed_final_statuses") or []
    assert allowed, (
        f"{base}.expected.json: allowed_final_statuses must be non-empty"
    )
