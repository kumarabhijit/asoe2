"""Phase 1 — CI guard: generated taxonomy constants must match the YAML.

This is the build-breaking test. If a developer edits the YAML and
forgets to regenerate, CI fails here. If a developer hand-edits the
generated file, CI fails here too. Same test, both directions.

Reference: docs/plans/case-intent-supergroup-implementation-plan.md §3,
acceptance criterion #11.
"""

from __future__ import annotations

import importlib

from scripts.generate_taxonomy_constants import PY_OUT, TS_OUT, generate


def test_generated_python_matches_yaml():
    """Compare bytes-on-disk vs bytes-from-generator so newline translation
    on the developer's platform cannot mask drift (code-review finding #1)."""
    py_text, _ = generate()
    actual = PY_OUT.read_bytes()
    assert actual == py_text.encode("utf-8"), (
        "contracts/_generated/taxonomy_constants.py is out of sync with "
        "db/seeds/case_taxonomy.yaml.\n\n"
        "Run: python -m scripts.generate_taxonomy_constants"
    )


def test_generated_typescript_matches_yaml():
    """Bytes-on-disk vs bytes-from-generator (see test above)."""
    _, ts_text = generate()
    actual = TS_OUT.read_bytes()
    assert actual == ts_text.encode("utf-8"), (
        "asoe-ui/src/generated/taxonomy.ts is out of sync with "
        "db/seeds/case_taxonomy.yaml.\n\n"
        "Run: python -m scripts.generate_taxonomy_constants"
    )


def test_generated_files_have_lf_line_endings():
    """Hard guard: committed files must use Unix LF endings. A CRLF file
    would round-trip through text-mode read and falsely pass — the
    bytes-comparison tests above already catch that, but a focused
    assertion makes the failure mode unambiguous."""
    py = PY_OUT.read_bytes()
    ts = TS_OUT.read_bytes()
    assert b"\r\n" not in py, "Python constants file has CRLF line endings"
    assert b"\r\n" not in ts, "TypeScript constants file has CRLF line endings"


def test_generator_is_deterministic():
    """Two consecutive generates produce byte-identical output."""
    py1, ts1 = generate()
    py2, ts2 = generate()
    assert py1 == py2
    assert ts1 == ts2


def test_generated_constants_importable_and_consistent():
    """The generated module imports and exports what app code expects."""
    module = importlib.import_module("contracts._generated.taxonomy_constants")
    assert module.SG_NEEDS_TRIAGE == "SG_NEEDS_TRIAGE"
    assert module.SG_BLOCK_UNMAPPED == "SG_BLOCK_UNMAPPED"
    assert module.INT_UNMAPPED_PENDING_TAXONOMY == "INT_UNMAPPED_PENDING_TAXONOMY"
    assert module.INT_UNKNOWN == "INT_UNKNOWN"
    assert isinstance(module.TAXONOMY_VERSION, str)
    # Every code in SUPERGROUP_CODES has a per-origin bucket and vice versa.
    all_in_buckets = {c for codes in module.SUPERGROUPS_BY_ORIGIN.values() for c in codes}
    assert set(module.SUPERGROUP_CODES) == all_in_buckets
    # Every intent's parent supergroup is a known supergroup.
    for sg, intents in module.INTENTS_BY_SUPERGROUP.items():
        assert sg in module.SUPERGROUP_CODES
        for code in intents:
            assert code in module.INTENT_CODES
