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
# Introspection lock: the dispatch entrypoint has no super-group input
# (acceptance criterion #7)
# ---------------------------------------------------------------------------
#
# An earlier version of this file held a "functional invariance" test
# that called a single-arg dispatch helper twice with identical inputs
# and asserted equal output — a tautology. The honest lock is
# introspective: the actual dispatch function in
# ``orchestration/nodes.py`` (``select_recipe`` / ``propose_recipe``)
# must accept no ``supergroup``-flavoured parameter. A future commit
# that adds one fails here.


def test_select_recipe_signature_has_no_supergroup_parameter():
    """``orchestration/nodes.py::select_recipe`` consumes ``GraphState``
    and that state object must not surface ``supergroup_code`` as a
    direct dispatch input. The static source-scan above already
    rejects any read of ``supergroup_code`` in routing layers; this
    test pins the function signature too."""
    import inspect

    from orchestration.nodes import select_recipe

    sig = inspect.signature(select_recipe)
    bad = [
        name for name in sig.parameters
        if "supergroup" in name.lower()
    ]
    assert not bad, (
        f"select_recipe() must not accept supergroup-flavoured params; "
        f"found {bad}"
    )


def test_recipe_spec_namespace_has_no_supergroup_attribute():
    """No instance of ``RecipeSpec`` in the registry carries a
    supergroup-flavoured attribute via runtime introspection (defence
    in depth on top of test_recipe_spec_has_no_supergroup_field which
    only looks at declared dataclass fields)."""
    for name, spec in REGISTRY.items():
        attrs = {a for a in dir(spec) if not a.startswith("_")}
        bad = {a for a in attrs if "supergroup" in a.lower()}
        assert not bad, (
            f"{name}: RecipeSpec instance carries supergroup-flavoured "
            f"attribute(s): {bad}"
        )
