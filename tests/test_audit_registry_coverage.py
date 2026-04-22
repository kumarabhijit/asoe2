"""Fitness test — every field on every `*AnalysisData` Pydantic class
must appear in `compliance/audit_bearing_registry.yaml`.

Purpose: prevent silent schema additions. Adding a field to a
Pydantic model without classifying it in the registry is a
compliance-visible gap — CODEOWNERS forces a compliance reviewer on
the registry change, and this test ensures the change actually
happened.

Invariants enforced:
  1. Every class whose name ends in 'AnalysisData' has a section in
     the registry.
  2. Every field on each class appears as a row under its section.
  3. Every row has a `tier` value from the allowed vocabulary.
  4. Conditional fields have a `depends_on` predicate.
  5. Grandfather-clause fields reference real registry rows.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Dict, Iterator, Type

import pytest
import yaml
from pydantic import BaseModel

import api.schemas as schemas_module

_REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent
    / "compliance"
    / "audit_bearing_registry.yaml"
)
_ALLOWED_TIERS = {"audit-bearing", "conditional", "contextual"}


def _load_registry() -> Dict[str, Any]:
    with _REGISTRY_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _iter_analysis_classes() -> Iterator[Type[BaseModel]]:
    """Every Pydantic class in api.schemas whose name ends
    'AnalysisData'. These are the types the UI data-presence pattern
    keys on; they must all be classified.
    """
    for name, obj in inspect.getmembers(schemas_module, inspect.isclass):
        if not name.endswith("AnalysisData"):
            continue
        if not issubclass(obj, BaseModel):
            continue
        if obj is BaseModel:
            continue
        yield obj


_REGISTRY = _load_registry()


def _field_rows(section: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Extract just the field-row entries (dicts with a `tier`). Skips
    section-level metadata that may live alongside fields."""
    return {
        name: entry
        for name, entry in section.items()
        if isinstance(entry, dict) and "tier" in entry
    }


# --------------------------------------------------------------------------
# Invariant 1+2: class coverage + field coverage.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cls", list(_iter_analysis_classes()), ids=lambda c: c.__name__,
)
def test_every_analysis_class_is_classified(cls: Type[BaseModel]):
    """Every `*AnalysisData` class must have a registry section with
    at least one classified field."""
    section = _REGISTRY.get(cls.__name__)
    assert section is not None, (
        f"{cls.__name__} is declared in api/schemas.py but has no "
        f"section in compliance/audit_bearing_registry.yaml. Adding a "
        f"new *AnalysisData class requires a registry row per field — "
        f"see prompts/pre_code_session.md §WORKING-STYLE CONSTRAINTS."
    )
    rows = _field_rows(section)
    assert rows, (
        f"{cls.__name__} has a registry section but zero field rows. "
        f"At least one row required."
    )


@pytest.mark.parametrize(
    "cls", list(_iter_analysis_classes()), ids=lambda c: c.__name__,
)
def test_every_field_has_a_registry_row(cls: Type[BaseModel]):
    """Every declared field on the Pydantic class must appear in the
    registry under that class's section. Catches silent schema
    additions (a new field shipped without compliance review)."""
    section = _REGISTRY.get(cls.__name__) or {}
    rows = _field_rows(section)
    missing = [f for f in cls.model_fields if f not in rows]
    assert not missing, (
        f"{cls.__name__} has fields {missing} not classified in the "
        f"registry. Add rows to compliance/audit_bearing_registry.yaml "
        f"(compliance review required via CODEOWNERS)."
    )


# --------------------------------------------------------------------------
# Invariant 3: tier values are within the allowed vocabulary.
# --------------------------------------------------------------------------


def test_every_row_has_valid_tier():
    bad: list[tuple[str, str, str]] = []
    for cls_name, section in _REGISTRY.items():
        if not isinstance(section, dict):
            continue
        for field_name, entry in section.items():
            if not isinstance(entry, dict) or "tier" not in entry:
                continue
            tier = entry.get("tier")
            if tier not in _ALLOWED_TIERS:
                bad.append((cls_name, field_name, tier))
    assert not bad, (
        f"Rows with invalid tier values: {bad}. "
        f"Allowed: {sorted(_ALLOWED_TIERS)}."
    )


# --------------------------------------------------------------------------
# Invariant 4: conditional rows must carry a depends_on predicate.
# --------------------------------------------------------------------------


def test_conditional_rows_have_depends_on():
    missing_predicate: list[str] = []
    for cls_name, section in _REGISTRY.items():
        if not isinstance(section, dict):
            continue
        for field_name, entry in section.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("tier") != "conditional":
                continue
            if not entry.get("depends_on"):
                missing_predicate.append(f"{cls_name}.{field_name}")
    assert not missing_predicate, (
        f"Conditional rows without depends_on predicate: "
        f"{missing_predicate}. The composer cannot evaluate a "
        f"conditional field without a predicate — "
        f"see api/analysis_composer.py::_predicate_holds for "
        f"supported shapes."
    )


# --------------------------------------------------------------------------
# Invariant 5: grandfather-clause references point at real rows.
# --------------------------------------------------------------------------


def test_grandfather_clause_fields_reference_real_registry_rows():
    dangling: list[str] = []
    clauses = _REGISTRY.get("grandfather_clauses", {}) or {}
    for clause_name, clause in clauses.items():
        for dotted in clause.get("fields", []):
            try:
                cls_name, field_name = dotted.split(".", 1)
            except ValueError:
                dangling.append(f"{clause_name}: malformed '{dotted}'")
                continue
            section = _REGISTRY.get(cls_name)
            if not isinstance(section, dict) or field_name not in section:
                dangling.append(f"{clause_name}: {dotted} not in registry")
    assert not dangling, (
        f"Grandfather clauses reference fields that don't exist in "
        f"the registry: {dangling}"
    )


# --------------------------------------------------------------------------
# Summary block matches reality.
# --------------------------------------------------------------------------


def test_summary_tally_matches_actual_classifications():
    """The `summary:` block is release-notes metadata. Keep it
    honest — it must match a live count of rows by tier."""
    summary = _REGISTRY.get("summary") or {}
    audit_bearing = 0
    conditional = 0
    contextual = 0
    for cls_name, section in _REGISTRY.items():
        if not isinstance(section, dict):
            continue
        for _name, entry in section.items():
            if not isinstance(entry, dict) or "tier" not in entry:
                continue
            tier = entry["tier"]
            if tier == "audit-bearing":
                audit_bearing += 1
            elif tier == "conditional":
                conditional += 1
            elif tier == "contextual":
                contextual += 1

    actual_total = audit_bearing + conditional + contextual
    assert summary.get("total_fields") == actual_total
    assert summary.get("audit_bearing") == audit_bearing
    assert summary.get("conditional") == conditional
    assert summary.get("contextual") == contextual
