"""Phase 1 — invariants that hold for any valid taxonomy seed.

These are pure-data tests against the YAML; they don't touch the DB. They
fail loudly when a future Steward edit breaks a structural property.

Reference: docs/specs/case-intent-supergroup-requirements.md §6, §9.2;
docs/plans/case-intent-supergroup-implementation-plan.md §3.
"""

from __future__ import annotations

from scripts.seed_taxonomy import load_yaml


def test_every_intent_references_known_supergroup():
    data = load_yaml()
    sg_codes = {sg["code"] for sg in data["supergroups"]}
    for intent in data["intents"]:
        assert intent["supergroup_code"] in sg_codes, (
            f"intent {intent['code']} -> {intent['supergroup_code']} (unknown)"
        )


def test_every_label_references_known_code():
    data = load_yaml()
    sg_codes = {sg["code"] for sg in data["supergroups"]}
    int_codes = {i["code"] for i in data["intents"]}
    for label in data["labels"]:
        if label["domain"] == "SUPERGROUP":
            assert label["code"] in sg_codes, f"label {label['code']} (SUPERGROUP) has no sg"
        else:
            assert label["code"] in int_codes, f"label {label['code']} (INTENT) has no intent"


def test_no_duplicate_codes():
    data = load_yaml()
    sg_codes = [sg["code"] for sg in data["supergroups"]]
    int_codes = [i["code"] for i in data["intents"]]
    assert len(sg_codes) == len(set(sg_codes)), "duplicate SG codes"
    assert len(int_codes) == len(set(int_codes)), "duplicate INT codes"


def test_naming_convention_prefixes():
    """Requirement §9.2: SG_* / INT_* SCREAMING_SNAKE_CASE."""
    import re
    sg_pat = re.compile(r"^SG_[A-Z][A-Z0-9_]*$")
    int_pat = re.compile(r"^INT_[A-Z][A-Z0-9_]*$")
    data = load_yaml()
    for sg in data["supergroups"]:
        assert sg_pat.match(sg["code"]), f"bad supergroup code: {sg['code']!r}"
    for intent in data["intents"]:
        assert int_pat.match(intent["code"]), f"bad intent code: {intent['code']!r}"


def test_no_shared_suffix_across_sg_and_int():
    """Requirement §9.2: lint rule banning SG_FOO + INT_FOO."""
    data = load_yaml()
    sg_suffix = {sg["code"].removeprefix("SG_") for sg in data["supergroups"]}
    int_suffix = {i["code"].removeprefix("INT_") for i in data["intents"]}
    collisions = sg_suffix & int_suffix
    assert not collisions, f"SG_/INT_ suffix collision: {sorted(collisions)}"


def test_unique_sap_block_code_per_sales_org():
    """A given (sap_block_code, sap_sales_org) maps to one intent."""
    data = load_yaml()
    seen: dict[tuple[str, str], str] = {}
    for intent in data["intents"]:
        sbc = intent.get("sap_block_code")
        if not sbc:
            continue
        org = intent.get("sap_sales_org") or ""
        key = (sbc, org)
        assert key not in seen, (
            f"SAP block code collision: {key} -> {seen[key]!r} vs {intent['code']!r}"
        )
        seen[key] = intent["code"]


def test_supergroup_owner_roles_are_lowercase_snake():
    """Owner role values are normalised — UI may render them as-is."""
    import re
    pat = re.compile(r"^[a-z][a-z0-9_]*$")
    data = load_yaml()
    for sg in data["supergroups"]:
        assert pat.match(sg["owner_role"]), f"bad owner_role: {sg['owner_role']!r}"


def test_reserved_codes_have_expected_supergroups():
    """Sentinel intents must point at sentinel supergroups."""
    data = load_yaml()
    by_code = {i["code"]: i["supergroup_code"] for i in data["intents"]}
    assert by_code["INT_UNMAPPED_PENDING_TAXONOMY"] == "SG_BLOCK_UNMAPPED"
    assert by_code["INT_UNKNOWN"] == "SG_NEEDS_TRIAGE"


def test_origin_distribution_matches_requirement():
    """Requirement §6.2 (8 API) + §6.3 (12 CUSTOMER) = 20 active rows."""
    data = load_yaml()
    counts: dict[str, int] = {}
    for sg in data["supergroups"]:
        counts[sg["origin"]] = counts.get(sg["origin"], 0) + 1
    assert counts.get("API", 0) == 8, counts
    assert counts.get("CUSTOMER", 0) == 12, counts
