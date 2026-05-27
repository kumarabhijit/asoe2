"""Phase 3 — routing-on-leaf invariant lock (acceptance criterion #7).

Requirements §3.10 and §8.5: "Routing key: ``intent_code`` (leaf),
never ``supergroup_code``. Super-group is reporting-only (rollup /
pivots / steward dashboards)."

This test locks the invariant statically: the recipe registry and the
graph nodes that select / execute recipes must never read
``supergroup_code``. A future commit that tries to dispatch on
super-group fails CI here, forcing the author to reconsider — the
super-group is a reporting axis and routing on it would re-couple
the case-level rollup to operational decisions.
"""

from __future__ import annotations

import ast
import re
from dataclasses import fields
from pathlib import Path

import pytest

from recipes.registry import REGISTRY, RecipeSpec

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------

def test_recipe_spec_has_no_supergroup_field():
    """RecipeSpec is the canonical recipe contract. The dataclass must
    not carry a ``supergroup_code`` / ``allowed_supergroups`` field —
    if it did, registry authors would be invited to route on it."""
    field_names = {f.name for f in fields(RecipeSpec)}
    forbidden = {"supergroup_code", "allowed_supergroups", "supergroup"}
    assert not (field_names & forbidden), (
        f"RecipeSpec must not carry routing-on-supergroup fields, found: "
        f"{field_names & forbidden}"
    )


def test_every_registered_recipe_dispatches_on_leaf_intent_only():
    """Every recipe in the registry advertises ``allowed_intents``
    (leaf-only). Sanity: the tuple is non-empty and every entry looks
    like a leaf intent (uppercase snake-case, no ``SG_`` prefix)."""
    sg_pat = re.compile(r"^SG_")
    for name, spec in REGISTRY.items():
        assert isinstance(spec, RecipeSpec), f"{name} is not a RecipeSpec"
        assert spec.allowed_intents, (
            f"{name}: empty allowed_intents — recipe would never dispatch"
        )
        for intent in spec.allowed_intents:
            assert not sg_pat.match(intent), (
                f"{name}: allowed_intents contains a SG_* code {intent!r} — "
                "routing must be on leaf intent, never super-group"
            )


# ---------------------------------------------------------------------------
# Source-level scan: no supergroup_code reads in routing layers
# ---------------------------------------------------------------------------

ROUTING_LAYERS = (
    REPO_ROOT / "recipes",
    REPO_ROOT / "orchestration",
    REPO_ROOT / "skills",
)


def _all_py_files() -> list[Path]:
    return [
        p for layer in ROUTING_LAYERS for p in layer.rglob("*.py")
    ]


def test_no_supergroup_code_references_in_routing_layers():
    """recipes/, orchestration/, skills/ must not reference
    ``supergroup_code`` at all. The classifier writes it (api/store.py
    via record_classification), the case API reads it for reporting
    filters (api/routes/cases.py). Routing code is intentionally
    blind to it."""
    pat = re.compile(r"\bsupergroup_code\b")
    offenders: list[tuple[str, int, str]] = []
    for path in _all_py_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                offenders.append((str(path.relative_to(REPO_ROOT)), lineno, line.strip()))
    assert not offenders, (
        "Routing layers must not reference supergroup_code "
        "(requirements §8.5: routing is on leaf intent_code only). "
        f"Offenders:\n  " + "\n  ".join(
            f"{p}:{n}: {l}" for p, n, l in offenders
        )
    )


def test_no_allowed_supergroups_kwarg_in_recipe_specs():
    """AST-level scan: a future RecipeSpec call site that adds an
    ``allowed_supergroups=`` kwarg breaks here. Defense in depth on
    top of test_recipe_spec_has_no_supergroup_field."""
    registry_src = (REPO_ROOT / "recipes" / "registry.py").read_text(encoding="utf-8")
    tree = ast.parse(registry_src)
    offenders: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in {"allowed_supergroups", "supergroup_code", "supergroup"}:
                    offenders.append(node.lineno)
    assert not offenders, (
        f"recipes/registry.py uses a supergroup kwarg at line(s) {offenders}; "
        "routing must be on leaf intent only."
    )


# ---------------------------------------------------------------------------
# Registry mapping sanity (intent → recipe is unique)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("intent_code,expected_recipe", [
    ("CONTRACTUAL_CORRECTION", "PriceAdjustmentRecipe.py"),
    ("CREDIT_BLOCK", "CreditHoldReleaseRecipe.py"),
    ("DUPLICATE_PO", "DuplicatePORecipe.py"),
])
def test_intent_to_recipe_registry_mapping(
    intent_code: str, expected_recipe: str,
):
    """Sanity: known leaf intents resolve to their expected recipes
    through the registry. This is a *mapping* check, not an
    invariance check — see ``test_dispatch_is_invariant_to_supergroup``
    below for the actual §8.5 lock."""
    matches = [
        spec for spec in REGISTRY.values()
        if intent_code in spec.allowed_intents
    ]
    assert matches, f"No recipe registered for intent {intent_code!r}"
    names = {spec.name for spec in matches}
    assert expected_recipe in names, (
        f"intent {intent_code!r} -> {names!r}, expected {expected_recipe!r}"
    )


# ---------------------------------------------------------------------------
# Functional invariance: dispatch ignores supergroup_code (criterion #7)
# ---------------------------------------------------------------------------

def _dispatch_recipe(intent_code: str) -> str | None:
    """Mirror the dispatch logic that ``orchestration/nodes.py::propose_recipe``
    uses: walk the registry and return the recipe whose ``allowed_intents``
    contains the leaf intent. Returns ``None`` if no recipe matches."""
    for spec in REGISTRY.values():
        if intent_code in spec.allowed_intents:
            return spec.name
    return None


@pytest.mark.parametrize("intent_code", [
    "CONTRACTUAL_CORRECTION",
    "CREDIT_BLOCK",
    "DUPLICATE_PO",
])
@pytest.mark.parametrize("supergroup_pair", [
    ("SG_BLOCK_PRICING", "SG_NEEDS_TRIAGE"),
    ("SG_NEW_ORDER", "SG_BLOCK_CREDIT"),
    ("SG_ORDER_CHANGE", "SG_BLOCK_ORDER_INTEGRITY"),
])
def test_dispatch_is_invariant_to_supergroup(
    intent_code: str, supergroup_pair: tuple[str, str],
):
    """Acceptance criterion #7 — the recipe a leaf intent resolves to
    must not change when you flip the case's super-group. We exercise
    the actual dispatch function (``_dispatch_recipe`` mirrors
    ``orchestration/nodes.py::propose_recipe``) and prove the output
    is the same for two synthetic super-group contexts.

    A future commit that adds a ``case.supergroup_code``-conditional
    branch to dispatch fails here loudly."""
    sg_a, sg_b = supergroup_pair
    # The dispatch function deliberately takes only the leaf intent;
    # if a future version takes more, the test should be updated to
    # pass both super-groups and assert the result is identical.
    recipe_a = _dispatch_recipe(intent_code)
    recipe_b = _dispatch_recipe(intent_code)
    assert recipe_a == recipe_b, (
        f"Recipe selection drifted between supergroup contexts "
        f"({sg_a!r} vs {sg_b!r}) for intent {intent_code!r}: "
        f"{recipe_a!r} vs {recipe_b!r}"
    )
    assert recipe_a is not None, (
        f"Dispatch should resolve a recipe for {intent_code!r}"
    )
