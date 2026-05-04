"""Fitness test — every gateway declared as a recipe dependency MUST
be registered in ``api/sandbox_gateways.py`` (sandbox runtime) and
``tests/conftest.py`` (pytest runtime).

Discovered by: asoe-ui#124 browser-e2e CI failure (asoe2#94 fixed it).

PR #93 added ``tenant_config`` as a GatewayDependency on
``DuplicatePORecipe.py`` and wired it into ``tests/conftest.py`` for
pytest, but missed wiring it into ``api/sandbox_gateways.py``. The
result: the live FastAPI sandbox server halted at
``resolve_dependencies`` with ``Gateway not registered: tenant_config``
on every DUPLICATE_PO event. PR #94 fixed it; this test prevents the
regression class.

Catches:

  1. A new GatewayDependency declared in ``recipes/registry.py`` whose
     ``gateway_name`` has no matching ``register_gateway(...)`` in
     ``api/sandbox_gateways.py``.
  2. Same gap in ``tests/conftest.py`` (the pytest equivalent).

Both are in scope because the two registration sites are independent
code paths that have to stay in lock-step. Either one missing the
registration breaks a different end-to-end consumer (sandbox server vs.
test pipeline).

Allow-list: gateways that legitimately don't need a sandbox stub
(e.g. a read-only metric collector that no recipe depends on yet) can
be added to ``_ALLOWED_UNREGISTERED_GATEWAYS`` with a justification.
The set is empty in V1 — every recipe-declared gateway is registered
in both sites.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Set

import pytest

from recipes.registry import REGISTRY


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SANDBOX_GATEWAYS_PATH = _REPO_ROOT / "api" / "sandbox_gateways.py"
_CONFTEST_PATH = _REPO_ROOT / "tests" / "conftest.py"

_ALLOWED_UNREGISTERED_GATEWAYS: Set[str] = set()


# ---------------------------------------------------------------------------
# Source-of-truth: gateway names declared as recipe dependencies
# ---------------------------------------------------------------------------


def _declared_gateway_names() -> Set[str]:
    """Every distinct ``gateway_name`` referenced by a GatewayDependency
    on a registered recipe."""
    names: Set[str] = set()
    for spec in REGISTRY.values():
        for dep in spec.dependencies:
            names.add(dep.gateway_name)
        # Effects also reference gateways — same registration story.
        for eff in spec.effects:
            names.add(eff.gateway_name)
    return names


# ---------------------------------------------------------------------------
# Reading registered names from each registration site
# ---------------------------------------------------------------------------


# Match `register_gateway(<expr>)` calls and extract the FIRST positional
# arg's source. We don't need to evaluate the expression — we just need
# the gateway name for comparison. The actual gateway-name string is
# either:
#   * The first arg of `StubGateway("foo", ...)`        → "foo"
#   * The `name` property of a real adapter class       → harder to
#     extract statically; we instead match the class name and rely on
#     a curated mapping.
# In practice the sandbox file uses `StubGateway("foo", ...)` for stubs
# and `TenantConfigGateway()` for the one real adapter. We handle both.

_REAL_GATEWAY_CLASS_NAMES: dict[str, str] = {
    # class name → gateway name (matches its `.name` property)
    "TenantConfigGateway": "tenant_config",
}


def _registered_gateway_names(source_path: Path) -> Set[str]:
    """Extract every gateway-name registered in the given source file.

    Walks the AST for calls to ``register_gateway(...)`` and pulls the
    name out of either:
      * ``StubGateway("foo", ...)``  →  "foo"
      * ``<KnownClassName>(...)``    →  curated mapping in
        _REAL_GATEWAY_CLASS_NAMES
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    names: Set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (
            (isinstance(node.func, ast.Name) and node.func.id == "register_gateway")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "register_gateway")
        ):
            continue
        if not node.args:
            continue
        arg = node.args[0]
        # Pattern 1: register_gateway(StubGateway("foo", ...))
        if (
            isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Name)
            and arg.func.id == "StubGateway"
            and arg.args
            and isinstance(arg.args[0], ast.Constant)
            and isinstance(arg.args[0].value, str)
        ):
            names.add(arg.args[0].value)
            continue
        # Pattern 2: register_gateway(<KnownClassName>(...))
        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
            cls_name = arg.func.id
            if cls_name in _REAL_GATEWAY_CLASS_NAMES:
                names.add(_REAL_GATEWAY_CLASS_NAMES[cls_name])
                continue
        # Pattern 3: an inline variable reference like a previously-bound
        # `oms_stub`. The conftest binds stubs to local names before
        # registering. Resolve those by matching the variable assignment
        # earlier in the same module.
        if isinstance(arg, ast.Name):
            varname = arg.id
            resolved = _resolve_stub_variable(tree, varname)
            if resolved:
                names.add(resolved)
    return names


def _resolve_stub_variable(tree: ast.AST, varname: str) -> str | None:
    """Find ``<varname> = StubGateway("foo", ...)`` in ``tree`` and
    return ``"foo"``."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == varname for t in node.targets):
            continue
        value = node.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "StubGateway"
            and value.args
            and isinstance(value.args[0], ast.Constant)
            and isinstance(value.args[0].value, str)
        ):
            return value.args[0].value
    return None


# ---------------------------------------------------------------------------
# Fitness assertions
# ---------------------------------------------------------------------------


_DECLARED = _declared_gateway_names() - _ALLOWED_UNREGISTERED_GATEWAYS
_SANDBOX_REGISTERED = _registered_gateway_names(_SANDBOX_GATEWAYS_PATH)
_CONFTEST_REGISTERED = _registered_gateway_names(_CONFTEST_PATH)


def test_declared_gateway_set_is_non_empty():
    """Sanity — the recipe registry actually declares some gateways.

    Without this, a future refactor that moves all gateway dependencies
    elsewhere would silently make the coverage tests pass vacuously.
    """
    assert _DECLARED, (
        "recipes/registry.py declares zero gateway dependencies. "
        "Has the fixture extraction stopped working?"
    )


@pytest.mark.parametrize("gateway_name", sorted(_DECLARED))
def test_every_declared_gateway_is_registered_in_sandbox(gateway_name: str):
    """Every gateway declared as a recipe dependency must be registered
    in api/sandbox_gateways.py — otherwise the live FastAPI sandbox
    server halts at resolve_dependencies on any event using that
    recipe (the asoe-ui#124 / asoe2#94 regression class).
    """
    assert gateway_name in _SANDBOX_REGISTERED, (
        f"Gateway {gateway_name!r} is declared as a dependency in "
        f"recipes/registry.py but NOT registered in "
        f"api/sandbox_gateways.py. Add a register_gateway(...) call "
        f"there alongside the existing stubs. Without this, any "
        f"event whose recipe depends on this gateway will halt with "
        f"'Gateway not registered: {gateway_name}' in the live "
        f"sandbox server (this is the bug that broke the asoe-ui "
        f"e2e in #124 — fixed by asoe2#94)."
    )


@pytest.mark.parametrize("gateway_name", sorted(_DECLARED))
def test_every_declared_gateway_is_registered_in_conftest(gateway_name: str):
    """Same coverage check for the pytest pipeline. tests/conftest.py
    is the pytest-side analog of api/sandbox_gateways.py — both have
    to stay in lock-step or the pytest suite green-lights changes that
    break the live server (or vice-versa).
    """
    assert gateway_name in _CONFTEST_REGISTERED, (
        f"Gateway {gateway_name!r} is declared as a dependency in "
        f"recipes/registry.py but NOT registered in "
        f"tests/conftest.py. Add a register_gateway(...) call there "
        f"alongside the existing stubs so pytest exercises the same "
        f"resolution path the live server runs."
    )


def test_sandbox_and_conftest_registrations_agree():
    """The two registration sites should declare the SAME gateway set
    (modulo any allow-listed extras). A divergence means a gateway is
    available in one runtime but not the other — exactly the class of
    drift this test catches before it ships."""
    sandbox_only = _SANDBOX_REGISTERED - _CONFTEST_REGISTERED
    conftest_only = _CONFTEST_REGISTERED - _SANDBOX_REGISTERED
    assert not sandbox_only and not conftest_only, (
        f"Gateway registration drift between sandbox and conftest:\n"
        f"  in api/sandbox_gateways.py only: {sorted(sandbox_only)}\n"
        f"  in tests/conftest.py only: {sorted(conftest_only)}\n"
        f"Both registration sites should be in lock-step. Add the "
        f"missing registrations on the trailing side."
    )


def test_no_orphan_gateway_registrations():
    """Sandbox / conftest should NOT register gateways that no recipe
    depends on (modulo allow-list). Orphan registrations are dead code
    and signal a recipe that was retired without cleaning up its
    sandbox stub."""
    declared_plus_allowlist = _DECLARED | _ALLOWED_UNREGISTERED_GATEWAYS
    orphans_sandbox = _SANDBOX_REGISTERED - declared_plus_allowlist
    orphans_conftest = _CONFTEST_REGISTERED - declared_plus_allowlist
    # Soft-warn rather than hard-fail: orphans during a partial migration
    # are legitimate, and a hard-fail would block legitimate WIP. Use
    # pytest.fail with a clear message that reviewers will see; promote
    # to a hard assertion once the codebase is steady-state.
    if orphans_sandbox or orphans_conftest:
        pytest.fail(
            f"Orphan gateway registrations (no recipe depends on them):\n"
            f"  api/sandbox_gateways.py: {sorted(orphans_sandbox)}\n"
            f"  tests/conftest.py: {sorted(orphans_conftest)}\n"
            f"Either re-add a recipe dependency on each, or remove the "
            f"registration."
        )
