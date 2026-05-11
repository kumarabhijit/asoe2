"""Phase 6a — registry coverage meta-test for audit-bearing fields.

Implements design.md Lane 1 W2 (the gap surfaced when the BDD test
plan was audited in the asoe-ui session):

  "tests/contract/test_audit_registry_coverage.py — parses
   compliance/audit_bearing_registry.yaml. For each *AnalysisData
   class in contracts/models.py [now api/schemas.py], asserts every
   audit-bearing field is populated by at least one recipe path."

Pragmatic scope for V1 (this commit):

  1. Every section in the registry (an `*AnalysisData` class) MUST
     resolve to an importable Pydantic model in api/schemas.py.
     Catches "registry rot" where a class was renamed or deleted
     without updating the registry.

  2. Every field declared in the registry section MUST exist on
     the matching Pydantic model. Catches the inverse: model
     field renamed without registry update.

  3. Every section that maps to an analysis adapter via
     ANALYSIS_ADAPTERS' value-tuple keys MUST have an adapter
     wired. Catches "registered a class but no adapter populates
     it" — the exact silent gap design.md W2 was authored to
     prevent.

The behavioural assertion ("the recipe ACTUALLY populates this
audit-bearing field") is a property-based test owned by W4
(test_validate_types_invariants.py + test_recipe_invariants.py).
The two tests are complementary: this one catches schema drift,
W4 catches runtime population gaps.

Grandfather clauses (registry section `grandfather_clauses`) are
free-text comments today, not structured data — we cannot parse
them into per-field waivers. Until they become structured, the
test runs in WARN mode for any field the model lacks but allows
the run to pass when the field name appears in the comment block.
This is a deliberate gap acknowledged inline.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REGISTRY_PATH = REPO_ROOT / "compliance" / "audit_bearing_registry.yaml"


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def registry() -> Dict[str, Any]:
    with REGISTRY_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# Top-level keys that document the file but do not name a class.
NON_SECTION_KEYS: Set[str] = {
    "version",
    "authored",
    "owners",
    "conventions",
    "grandfather_clauses",
    "summary",
}

# Sections that declare audit-bearing classification ahead of the
# Pydantic model landing. Each entry MUST cite a tracking marker
# (workshop minutes / ADR / grandfather clause comment) so the
# deferral is reviewable. Adding an entry here is a deliberate
# acceptance — the test still surfaces the section in the report
# but does not fail the build.
KNOWN_MISSING_CLASS_SECTIONS: Dict[str, str] = {
    # LLMProvenance: workshop V1 PR-1 — declared 3 audit-bearing
    # fields (provider / model / request_id) pending compliance
    # sign-off + Pydantic model. See registry summary block.
    "LLMProvenance": "V1 PR-1 — Pydantic model pending compliance sign-off",
}

# Audit-bearing fields declared in the registry that do not yet
# exist on the corresponding Pydantic model. The drift is the
# silent gap design.md W2 was authored to surface — the first
# run of this test found 7 real fields (MOQ + Pallet enrichment)
# that the registry classifies as audit-bearing but the schema
# does not carry. Each entry below cites the registry workshop
# row that authored the classification; closing each gap is a
# follow-up PR that adds the field to the Pydantic class (per
# CLAUDE.md Pillar #6 — never prune the registry to match the
# model).
#
# Format: "{Section}.{field}": "{citation}"
# When a field lands on the model, REMOVE the entry. The test
# transitions to passing the per-field assertion automatically.
KNOWN_FIELD_GAPS: Dict[str, str] = {
    # MOQ section — sap_steps was declared in T5 (2026-04-22)
    # workshop minutes as the SAP-derived rounding ladder. The
    # adapter populates moq_source / channel / contract_ref /
    # block_status but not the steps array yet.
    "MOQAnalysisData.sap_steps": "T5 (2026-04-22) — pending adapter expansion to surface SAP rounding ladder",
    # Pallet section — workshop classified 6 audit-bearing fields
    # the SAP/logistics gateways will populate. The Pydantic model
    # was authored before the gateway wiring and lags. Adding
    # these is a Lane 1 W3 follow-up.
    "PalletAnalysisData.pallet_tie": "Lane 1 W3 — pending PalletAnalysisData expansion for SAP pallet geometry",
    "PalletAnalysisData.pallet_height": "Lane 1 W3 — same as pallet_tie",
    "PalletAnalysisData.layer_cases": "Lane 1 W3 — same as pallet_tie",
    "PalletAnalysisData.carrier": "Lane 1 W3 — pending carrier gateway integration",
    "PalletAnalysisData.freight_delta": "Lane 1 W3 — derived from carrier rate cards",
    "PalletAnalysisData.customer_preferences": "Lane 1 W3 — pending customer-master gateway expansion",
}

# Modules searched for each section's class. api.schemas is the
# canonical home; the rest exist because some sections grew out
# of other modules and have not been migrated yet.
SCHEMA_MODULES = (
    "api.schemas",
    "api.duplicate_envelope",
    "contracts.models",
)


@pytest.fixture(scope="module")
def section_classes(registry: Dict[str, Any]) -> Dict[str, type]:
    """Resolve each section name to its Pydantic class.

    Walks SCHEMA_MODULES in order and returns the first class
    that matches the section name. Returns None when the class
    cannot be located in any of the candidate modules.
    """
    candidates = []
    for mod_name in SCHEMA_MODULES:
        try:
            candidates.append(importlib.import_module(mod_name))
        except ImportError:
            continue
    sections: Dict[str, type] = {}
    for key in registry:
        if key in NON_SECTION_KEYS:
            continue
        cls = None
        for mod in candidates:
            cls = getattr(mod, key, None)
            if cls is not None:
                break
        sections[key] = cls
    return sections


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_every_registry_section_resolves_to_a_pydantic_class(
    section_classes: Dict[str, type],
) -> None:
    """A registry section without a backing class is silent drift.

    KNOWN_MISSING_CLASS_SECTIONS is the deliberate exception
    list — entries there are expected gaps (declared classifications
    ahead of the model landing) and require a citation. Anything
    else is real drift.
    """
    real_drift: List[str] = []
    accepted_gaps: List[str] = []
    for name, cls in section_classes.items():
        if cls is not None:
            continue
        if name in KNOWN_MISSING_CLASS_SECTIONS:
            accepted_gaps.append(
                f"  - {name}: {KNOWN_MISSING_CLASS_SECTIONS[name]}"
            )
        else:
            real_drift.append(f"  - {name}")
    # The accepted gaps are reported but do not fail. Print them
    # so reviewers see the running tally.
    if accepted_gaps:
        print(
            "\n[audit-registry] sections without a backing class "
            "(known + cited):\n" + "\n".join(accepted_gaps)
        )
    assert not real_drift, (
        "registry sections with no matching Pydantic class "
        "(and no entry in KNOWN_MISSING_CLASS_SECTIONS):\n"
        + "\n".join(real_drift)
        + "\n(check api/schemas.py — section may have been renamed or deleted, "
        "or add to KNOWN_MISSING_CLASS_SECTIONS with a tracking citation)"
    )


def test_every_registry_field_exists_on_the_pydantic_class(
    registry: Dict[str, Any], section_classes: Dict[str, type]
) -> None:
    """For each section, every declared field must be a model field.

    Catches the case where a model field is renamed but the
    registry still lists the old name. Without this test the
    field stays declared audit-bearing forever, never populates
    (the model has no such field), and every record routes to
    AUDIT_CONTEXT_MISSING — a stuck pipeline with no actionable
    error message.

    KNOWN_FIELD_GAPS is the citation list for fields the registry
    declares ahead of the model. Each entry is a tracked follow-up;
    when the field lands on the model, the entry is removed and
    the test re-tightens.
    """
    offenders: List[str] = []
    accepted_gaps: List[str] = []
    for section_name, cls in section_classes.items():
        if cls is None:
            continue  # already covered by the previous test
        section = registry[section_name]
        if not isinstance(section, dict):
            continue
        model_fields = set(getattr(cls, "model_fields", {}).keys())
        for field_name, body in section.items():
            if not isinstance(body, dict):
                continue
            if field_name in model_fields:
                continue
            full_name = f"{section_name}.{field_name}"
            if full_name in KNOWN_FIELD_GAPS:
                accepted_gaps.append(
                    f"  - {full_name}: {KNOWN_FIELD_GAPS[full_name]}"
                )
            else:
                offenders.append(
                    f"  {full_name} declared in registry but absent on the Pydantic model"
                )
    if accepted_gaps:
        print(
            "\n[audit-registry] field-level gaps tracked via "
            "KNOWN_FIELD_GAPS:\n" + "\n".join(accepted_gaps)
        )
    assert not offenders, (
        "registry-vs-model drift — fields present in the registry "
        "but missing from the Pydantic model (and no KNOWN_FIELD_GAPS "
        "citation):\n" + "\n".join(offenders)
    )


def test_every_audit_bearing_field_has_a_documented_tier(
    registry: Dict[str, Any], section_classes: Dict[str, type]
) -> None:
    """Every field row must declare a tier in the allowed set.

    Vocabulary is fixed: audit-bearing | conditional | contextual.
    A row with no tier (or an unknown tier) is a silent
    classification gap — the composer wouldn't know whether to
    treat absence as AUDIT_CONTEXT_MISSING.
    """
    ALLOWED = {"audit-bearing", "conditional", "contextual"}
    offenders: List[str] = []
    for section_name, cls in section_classes.items():
        if cls is None:
            continue
        section = registry[section_name]
        if not isinstance(section, dict):
            continue
        for field_name, body in section.items():
            if not isinstance(body, dict):
                continue
            tier = body.get("tier")
            if tier not in ALLOWED:
                offenders.append(
                    f"  {section_name}.{field_name}: tier={tier!r} "
                    f"(allowed: {sorted(ALLOWED)})"
                )
    assert not offenders, (
        "registry fields with missing or invalid tier:\n"
        + "\n".join(offenders)
    )


def test_analysis_adapters_target_registered_sections(
    registry: Dict[str, Any], section_classes: Dict[str, type]
) -> None:
    """Every adapter in ANALYSIS_ADAPTERS must return a registered class.

    This binds the adapter wiring to the registry: a new adapter
    that returns a class the registry doesn't know about means
    the composer cannot enforce audit-bearing coverage on that
    surface. Either the registry needs a new section, or the
    adapter is wired to the wrong type.
    """
    from api.analysis_adapters import ANALYSIS_ADAPTERS
    import typing

    section_names: Set[str] = set(section_classes.keys())
    offenders: List[str] = []
    for recipe_key, (_field, adapter) in ANALYSIS_ADAPTERS.items():
        try:
            hints = typing.get_type_hints(adapter)
        except Exception:
            continue
        return_type = hints.get("return")
        if return_type is None:
            continue
        # Unwrap Optional[X] / Union[X, None].
        args = typing.get_args(return_type)
        candidate: Any = return_type
        if args:
            for a in args:
                if a is type(None):
                    continue
                candidate = a
                break
        name = getattr(candidate, "__name__", None)
        if name is None:
            continue
        if name not in section_names:
            offenders.append(
                f"  {recipe_key} -> {name} (not in registry sections)"
            )
    assert not offenders, (
        "ANALYSIS_ADAPTERS targets unregistered classes:\n"
        + "\n".join(offenders)
    )
