"""Steward CLI — taxonomy proposal workflow tests.

Authority: docs/specs/case-intent-supergroup-requirements.md §9.
Each test exercises a CLI subcommand against a *copy* of the live
seed YAML so changes don't leak across tests or pollute the repo's
committed state.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import scripts.generate_taxonomy_constants as gen
import scripts.seed_taxonomy as seed_mod
import scripts.steward_change as steward
from scripts.seed_taxonomy import load_yaml


@pytest.fixture(autouse=True)
def isolated_seed(tmp_path, monkeypatch):
    """Redirect SEED_PATH + generated-constants paths to tmp_path so
    each test gets a clean slate and no test ever rewrites the real
    seed / committed constants."""
    real_seed = seed_mod.SEED_PATH
    tmp_seed = tmp_path / "case_taxonomy.yaml"
    tmp_seed.write_text(real_seed.read_text(encoding="utf-8"), encoding="utf-8")
    tmp_py = tmp_path / "taxonomy_constants.py"
    tmp_ts = tmp_path / "taxonomy.ts"

    monkeypatch.setattr(seed_mod, "SEED_PATH", tmp_seed)
    monkeypatch.setattr(steward, "SEED_PATH", tmp_seed)
    monkeypatch.setattr(gen, "PY_OUT", tmp_py)
    monkeypatch.setattr(gen, "TS_OUT", tmp_ts)
    monkeypatch.setattr(steward, "PY_OUT", tmp_py)
    monkeypatch.setattr(steward, "TS_OUT", tmp_ts)

    # Generate the constants once so subsequent `validate` calls see
    # a synchronised starting state.
    py, ts = gen.generate()
    tmp_py.write_bytes(py.encode("utf-8"))
    tmp_ts.write_bytes(ts.encode("utf-8"))
    yield tmp_path


# ---------------------------------------------------------------------------
# add-intent
# ---------------------------------------------------------------------------

def test_add_intent_writes_to_yaml(isolated_seed: Path):
    rc = steward.main([
        "add-intent",
        "--code", "INT_PALLET_DAMAGE",
        "--supergroup", "SG_BLOCK_LOGISTICS",
        "--description", "Damaged pallet receipt rejection",
        "--sap-block-code", "ZD",
        "--sap-block-field", "LIFSP",
    ])
    assert rc == 0
    data = load_yaml()
    codes = {i["code"] for i in data["intents"]}
    assert "INT_PALLET_DAMAGE" in codes
    new = next(i for i in data["intents"] if i["code"] == "INT_PALLET_DAMAGE")
    assert new["supergroup_code"] == "SG_BLOCK_LOGISTICS"
    assert new["sap_block_code"] == "ZD"
    assert new["sap_block_field"] == "LIFSP"


def test_add_intent_rejects_duplicate(isolated_seed: Path):
    rc = steward.main([
        "add-intent",
        "--code", "INT_PRICE_MISMATCH",  # already in seed
        "--supergroup", "SG_BLOCK_PRICING",
        "--description", "duplicate code attempt",
    ])
    assert rc == 2


def test_add_intent_rejects_unknown_supergroup(isolated_seed: Path):
    rc = steward.main([
        "add-intent",
        "--code", "INT_NEW",
        "--supergroup", "SG_BOGUS",
        "--description", "test description",
    ])
    assert rc == 2


def test_add_intent_rejects_sap_code_without_field(isolated_seed: Path):
    """A SAP block code without its table field is the exact shape the
    reconciliation cron flags as malformed_in_db — reject at the CLI."""
    rc = steward.main([
        "add-intent",
        "--code", "INT_NEW_NO_FIELD",
        "--supergroup", "SG_BLOCK_PRICING",
        "--description", "missing sap_block_field",
        "--sap-block-code", "ZZ",
    ])
    assert rc == 2


def test_atomic_commit_leaves_files_untouched_on_validation_failure(
    isolated_seed: Path,
):
    """If a proposal is well-formed by the CLI's own checks but the
    JSON-schema validation in load_yaml fails (e.g. the steward
    deliberately corrupts the seed via a direct edit on the side and
    then runs add-intent), no file is mutated. The atomic-commit
    contract from review finding #3 says the YAML, the Python
    constants, and the TS constants are untouched on any failure."""
    import scripts.seed_taxonomy as seed_mod
    seed_path = seed_mod.SEED_PATH
    py_path = isolated_seed / "taxonomy_constants.py"
    ts_path = isolated_seed / "taxonomy.ts"
    yaml_before = seed_path.read_bytes()
    py_before = py_path.read_bytes()
    ts_before = ts_path.read_bytes()

    # Force a validation failure: corrupt the YAML on disk so the
    # add-intent flow's atomic_commit step fails at load_yaml(tmp).
    # The CLI reads + re-serialises so the tmp YAML inherits the
    # corruption, and validation rejects it.
    raw = seed_path.read_text(encoding="utf-8")
    # Inject a duplicate supergroup code → invariant violation.
    seed_path.write_text(
        raw + "\n  - code: SG_BLOCK_PRICING\n    origin: API\n"
              "    description: dup for test (10+ chars)\n"
              "    owner_role: x\n    sort_order: 9999\n",
        encoding="utf-8",
    )
    yaml_corrupted = seed_path.read_bytes()

    rc = steward.main([
        "add-intent",
        "--code", "INT_NEW_AFTER_CORRUPT",
        "--supergroup", "SG_BLOCK_PRICING",
        "--description", "irrelevant — validation will fail",
    ])
    assert rc == 3
    # The YAML on disk must be the corrupted-by-test state, NOT the
    # CLI's attempted edit on top. Constants must also be unchanged.
    assert seed_path.read_bytes() == yaml_corrupted
    assert py_path.read_bytes() == py_before
    assert ts_path.read_bytes() == ts_before
    # And no leftover ``.yaml.tmp`` sibling.
    tmp = seed_path.with_suffix(".yaml.tmp")
    assert not tmp.exists()

    # Restore the original YAML so other tests in this module are
    # unaffected (autouse fixture also restores, but be tidy).
    seed_path.write_bytes(yaml_before)


def test_add_intent_emits_default_label(isolated_seed: Path):
    """Without --display-name the CLI generates a humanised default
    label from the code, so the row immediately renders in the UI."""
    rc = steward.main([
        "add-intent",
        "--code", "INT_TAX_VAT_MISSING",
        "--supergroup", "SG_BLOCK_COMPLIANCE",
        "--description", "VAT ID missing on EU cross-border ship-to",
    ])
    assert rc == 0
    data = load_yaml()
    label = next(
        l for l in data["labels"]
        if l["code"] == "INT_TAX_VAT_MISSING" and l["domain"] == "INTENT"
    )
    assert label["display_name"] == "Tax Vat Missing"


def test_add_intent_regenerates_constants(isolated_seed: Path):
    """The CLI re-runs the constant generator so the committed
    contracts/_generated files don't drift."""
    rc = steward.main([
        "add-intent",
        "--code", "INT_NEW_REGEN_TEST",
        "--supergroup", "SG_BLOCK_PRICING",
        "--description", "test that constants regenerated",
    ])
    assert rc == 0
    py = isolated_seed / "taxonomy_constants.py"
    assert "INT_NEW_REGEN_TEST" in py.read_text()


# ---------------------------------------------------------------------------
# add-supergroup
# ---------------------------------------------------------------------------

def test_add_supergroup_writes_to_yaml(isolated_seed: Path):
    rc = steward.main([
        "add-supergroup",
        "--code", "SG_BLOCK_TAX",
        "--origin", "API",
        "--description", "Tax / VAT compliance block",
        "--owner-role", "tax_ops",
    ])
    assert rc == 0
    data = load_yaml()
    new = next(s for s in data["supergroups"] if s["code"] == "SG_BLOCK_TAX")
    assert new["origin"] == "API"
    assert new["owner_role"] == "tax_ops"
    # sort_order is auto-picked > max existing API sg sort_order.
    api_orders = [
        s["sort_order"] for s in data["supergroups"] if s["origin"] == "API"
    ]
    assert new["sort_order"] == max(o for o in api_orders if o != new["sort_order"]) + 10


def test_add_supergroup_rejects_duplicate(isolated_seed: Path):
    rc = steward.main([
        "add-supergroup",
        "--code", "SG_BLOCK_PRICING",
        "--origin", "API",
        "--description", "duplicate",
        "--owner-role", "x",
    ])
    assert rc == 2


# ---------------------------------------------------------------------------
# deprecate-intent
# ---------------------------------------------------------------------------

def test_deprecate_intent_stamps_date(isolated_seed: Path):
    from datetime import date
    rc = steward.main([
        "deprecate-intent",
        "--code", "INT_DELIVERY_DELAY",
        "--effective", "2026-06-01",
    ])
    assert rc == 0
    data = load_yaml()
    target = next(i for i in data["intents"] if i["code"] == "INT_DELIVERY_DELAY")
    assert target["deprecated_at"] == "2026-06-01"


def test_deprecate_intent_with_replacement_link(isolated_seed: Path):
    rc = steward.main([
        "deprecate-intent",
        "--code", "INT_PALLET_CONFIG",
        "--replaced-by", "INT_BROKEN_PALLET",
    ])
    assert rc == 0
    data = load_yaml()
    target = next(i for i in data["intents"] if i["code"] == "INT_PALLET_CONFIG")
    assert target["replaced_by_code"] == "INT_BROKEN_PALLET"


def test_deprecate_intent_rejects_unknown(isolated_seed: Path):
    rc = steward.main([
        "deprecate-intent", "--code", "INT_GHOST",
    ])
    assert rc == 2


def test_deprecate_intent_rejects_unknown_replacement(isolated_seed: Path):
    rc = steward.main([
        "deprecate-intent",
        "--code", "INT_DELIVERY_DELAY",
        "--replaced-by", "INT_DOES_NOT_EXIST",
    ])
    assert rc == 2


def test_deprecate_intent_rejects_already_deprecated(isolated_seed: Path):
    # First deprecation succeeds.
    assert steward.main([
        "deprecate-intent", "--code", "INT_DELIVERY_DELAY",
    ]) == 0
    # Second fails because deprecated_at is already set.
    rc = steward.main([
        "deprecate-intent", "--code", "INT_DELIVERY_DELAY",
    ])
    assert rc == 2


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

def test_validate_clean_initial_state(isolated_seed: Path):
    rc = steward.main(["validate"])
    assert rc == 0


def test_validate_detects_constants_drift(isolated_seed: Path):
    """A direct YAML edit without regenerating constants must surface
    here so the steward sees the drift before committing."""
    py = isolated_seed / "taxonomy_constants.py"
    py.write_bytes(b"# tampered\n")  # drift the generated file
    rc = steward.main(["validate"])
    assert rc == 4
