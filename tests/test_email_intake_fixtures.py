"""Parse + intake tests for the relocated `.eml` intake fixtures.

RFC: asoe-ui/docs/synthetic-data-placement-rfc.md (Decision A, Phase 4).

The 9 synthetic intake emails moved from asoe-ui (`data/synthetic/emails/`)
to `tests/fixtures/email_intake/`. They were *converted* (not copied) into
the `email_intake.fetch_message` payload shape via
`scripts/convert_eml_fixtures.py`. These tests assert:

  1. **Parse / drift** — every raw `.eml` re-derives byte-for-byte to the
     committed `*.fetch_message.json` (regenerate-then-diff, same pattern
     as the asoe-ui `verify:` gates). Edit a fixture without re-running the
     converter and this fails.
  2. **Audit-bearing fields** — each converted payload carries the three
     audit-bearing fetch_message keys (`source_email_id`, `from_address`,
     `received_at`) that `gateways/msgraph_intake.py` declares, non-empty.
  3. **Intake contract** — feeding the payload through the `email_intake`
     gateway port (StubGateway) round-trips it as a SUCCESS GatewayResponse,
     proving the converted fixture is a valid intake input to the recipe
     path that consumes `fetch_message`.
  4. **Manifest / catalog coverage** — the manifest, the raw `.eml` set, and
     the catalog `email_scenarios[].fixture` references stay in lockstep.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import yaml

from contracts.models import GatewayRequest, GatewayResponse
from gateways.stub import StubGateway
from scripts.convert_eml_fixtures import (
    FIXTURE_DIR,
    RAW_DIR,
    build_recorded,
    load_manifest,
    recorded_path,
)

# Audit-bearing keys for the fetch_message op, mirrored from
# gateways/msgraph_intake.py::_AUDIT_BEARING_FIELDS["fetch_message"].
_AUDIT_BEARING = ("source_email_id", "from_address", "received_at")

CATALOG_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "scenarios" / "catalog.yaml"

MANIFEST = load_manifest()


@pytest.mark.parametrize("entry", MANIFEST, ids=[e["file"] for e in MANIFEST])
def test_eml_converts_without_drift(entry):
    """Re-deriving the fixture from raw `.eml` matches the committed JSON."""
    expected = json.loads(recorded_path(entry).read_text())
    actual = build_recorded(entry)
    assert actual == expected, (
        f"{entry['file']} drifted from its committed fetch_message JSON — "
        f"re-run scripts/convert_eml_fixtures.py"
    )


@pytest.mark.parametrize("entry", MANIFEST, ids=[e["file"] for e in MANIFEST])
def test_audit_bearing_fields_present(entry):
    out = json.loads(recorded_path(entry).read_text())["output"]
    for key in _AUDIT_BEARING:
        assert out.get(key), f"{entry['file']}: audit-bearing fetch_message field {key!r} is empty"


@pytest.mark.parametrize("entry", MANIFEST, ids=[e["file"] for e in MANIFEST])
def test_fixture_feeds_email_intake_gateway(entry):
    """The converted payload is a valid `email_intake.fetch_message` return."""
    rec = json.loads(recorded_path(entry).read_text())
    out = rec["output"]
    gateway = StubGateway(
        "email_intake",
        responses={
            "fetch_message": GatewayResponse(
                gateway_name="email_intake", operation="fetch_message",
                status="SUCCESS", data=out,
            ),
        },
    )
    resp = gateway.execute(GatewayRequest(
        gateway_name="email_intake", operation="fetch_message",
        params={"source_email_id": out["source_email_id"]},
        trace_id="test-email-intake",
    ))
    assert resp.status == "SUCCESS"
    assert resp.data["source_email_id"] == out["source_email_id"]
    assert resp.data["from_address"] == out["from_address"]


def test_every_raw_eml_is_in_manifest():
    raw_files = {p.name for p in RAW_DIR.glob("*.eml")}
    manifest_files = {e["file"] for e in MANIFEST}
    assert raw_files == manifest_files, (
        f"raw/.eml set and manifest disagree: "
        f"only-in-raw={sorted(raw_files - manifest_files)}, "
        f"only-in-manifest={sorted(manifest_files - raw_files)}"
    )


def test_catalog_email_fixtures_exist():
    """Every catalog email_scenario that names a fixture points at a real `.eml`."""
    catalog = yaml.safe_load(CATALOG_PATH.read_text())
    referenced = {s["fixture"] for s in catalog.get("email_scenarios", []) if s.get("fixture")}
    missing = {f for f in referenced if not (RAW_DIR / f).exists()}
    assert not missing, f"catalog email_scenarios reference missing .eml fixtures: {sorted(missing)}"


def test_recorded_fixtures_are_committed():
    """A committed `*.fetch_message.json` exists for every manifest entry."""
    for entry in MANIFEST:
        assert recorded_path(entry).exists(), (
            f"missing converted fixture for {entry['file']} — "
            f"run scripts/convert_eml_fixtures.py"
        )
    # No orphan converted fixtures without a manifest entry.
    committed = {p.name for p in FIXTURE_DIR.glob("*.fetch_message.json")}
    expected = {recorded_path(e).name for e in MANIFEST}
    assert committed == expected, (
        f"orphan/missing converted fixtures: {sorted(committed ^ expected)}"
    )
