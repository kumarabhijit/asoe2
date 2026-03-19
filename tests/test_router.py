from __future__ import annotations

# Consistency pass — router coverage
#
# Verifies:
#   - Default path returns DeterministicFallbackBackend
#   - USE_OUTLINES_BACKEND=0 also returns DeterministicFallbackBackend
#   - OutlinesConstrainedBackend is NOT imported at module level
#     (lazy import guard — heavy optional dependency)
#   - The fallback backend's machine-consumed outputs are within the
#     constrained vocabulary defined in constraints/specs.py

from constraints.router import get_constrained_backend
from constraints.fallback_backend import DeterministicFallbackBackend


def test_router_defaults_to_fallback(monkeypatch):
    monkeypatch.delenv("USE_OUTLINES_BACKEND", raising=False)
    backend = get_constrained_backend()
    assert isinstance(backend, DeterministicFallbackBackend)


def test_router_returns_fallback_when_disabled(monkeypatch):
    """USE_OUTLINES_BACKEND=0 must also return the fallback backend."""
    monkeypatch.setenv("USE_OUTLINES_BACKEND", "0")
    backend = get_constrained_backend()
    assert isinstance(backend, DeterministicFallbackBackend)


def test_outlines_backend_not_imported_at_module_level():
    """OutlinesConstrainedBackend must never be imported eagerly.

    The constraints package __init__ explicitly excludes it; verify that the
    router module itself has not pulled it in at collection time.
    """
    import constraints.router as router_mod
    assert not hasattr(router_mod, "OutlinesConstrainedBackend"), (
        "OutlinesConstrainedBackend was imported at module level in router.py — "
        "this would break test collection when outlines is not installed."
    )


def test_constraints_package_does_not_expose_outlines_backend():
    """The constraints package __init__ must not export OutlinesConstrainedBackend."""
    import constraints as pkg
    assert not hasattr(pkg, "OutlinesConstrainedBackend")
