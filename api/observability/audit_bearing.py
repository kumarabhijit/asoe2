"""@audit_bearing decorator + source-level lint.

Marks a FastAPI route handler as a state-mutating, SOX-relevant
operation. Pure metadata — the decorator does not alter behaviour.
The point is that **every** state-mutating route under api/routes/
must be marked OR explicitly exempted; the regression test
``tests/test_otel_audit_bearing.py::TestAuditBearingLint`` enforces
the rule at CI time so a new POST endpoint can't silently bypass the
SOX trail.

Usage::

    @router.post("/foo")
    @audit_bearing(reason="creates a new exception override")
    async def foo(...):
        ...

Reason strings flow into the structured log payload (and from there
into Application Insights) so an auditor can ``WHERE audit_bearing_reason
== "..."`` to find every override-creation event without scanning the
codebase.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Callable, Iterable, List, Set

_ATTR_FLAG = "__asoe_audit_bearing__"
_ATTR_REASON = "__asoe_audit_bearing_reason__"


def audit_bearing(*, reason: str) -> Callable[[Callable], Callable]:
    """Decorator factory — tag a handler as audit-bearing.

    ``reason`` is required and must be a non-empty string. It is
    embedded in the function metadata for downstream logging /
    introspection.
    """
    if not reason:
        raise ValueError("audit_bearing requires a non-empty reason")

    def decorate(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)

        setattr(wrapper, _ATTR_FLAG, True)
        setattr(wrapper, _ATTR_REASON, reason)
        # Also mark the original so import-time introspection
        # (i.e. before decorator wrapping in the route registry) can
        # discover it.
        setattr(fn, _ATTR_FLAG, True)
        setattr(fn, _ATTR_REASON, reason)
        return wrapper

    return decorate


def is_audit_bearing(fn: Callable) -> bool:
    return bool(getattr(fn, _ATTR_FLAG, False))


def audit_bearing_reason(fn: Callable) -> str:
    return getattr(fn, _ATTR_REASON, "")


# ---------------------------------------------------------------------------
# Source-level lint
# ---------------------------------------------------------------------------

# Match the start of a state-mutating decorator on a single line. The
# path may be on the SAME line ("@router.post('/x')") or the FOLLOWING
# line ("@router.post(\n    '/x',\n    ..."); we resolve the path from
# the source window below.
_DECO_RE = re.compile(
    r'@(?:router|app)\.(?P<method>post|put|patch|delete)\b'
)
# Match an @audit_bearing decorator anywhere in the preceding window.
# Restricted to "@audit_bearing(" so a comment mentioning the word
# (e.g. "# TODO: add audit_bearing decorator") does NOT silently
# satisfy the lint.
_AUDIT_RE = re.compile(r"@audit_bearing\s*\(")
_PATH_RE = re.compile(r'["\'](?P<path>/[^"\']+)["\']')


def _strip_path_prefix(path: str) -> str:
    """Normalise the path so EXEMPT_PATHS can match without worrying
    about the router-mount prefix.
    """
    # Routers mount under /api/v1 or similar; we strip nothing and
    # let callers decide. Paths with leading-slash are returned as-is.
    return path if path.startswith("/") else f"/{path}"


def _extract_path(lines: List[str], decorator_idx: int) -> str | None:
    """Scan from the decorator line forward up to 5 lines, looking for
    the first quoted "/..." that is the route path. Returns the path
    string or None if not found (the lint then ignores the match —
    a route without a path is malformed anyway and will fail at boot)."""
    for offset in range(0, 6):
        if decorator_idx + offset >= len(lines):
            break
        m = _PATH_RE.search(lines[decorator_idx + offset])
        if m:
            return m.group("path")
    return None


def scan_routes_for_violations(
    *,
    routes_dir: Path,
    exempt_paths: Set[str] | Iterable[str] = frozenset(),
) -> List[str]:
    """Return a list of "<file>:<line>:<method> <path>" violations.

    A violation is a state-mutating route handler whose immediately
    preceding decorator stack does NOT include ``@audit_bearing(``,
    AND whose path is not in ``exempt_paths``.

    The check is intentionally source-level (a regex sweep) rather
    than runtime — it catches a new POST handler being added without
    the marker BEFORE the FastAPI app even imports. Multi-line
    ``@router.post(\\n  "/path", ...\\n)`` decorators are handled by
    scanning forward up to 5 lines for the path string.
    """
    exempt = set(exempt_paths)
    violations: List[str] = []

    for path in sorted(routes_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        lines = path.read_text().splitlines()
        for idx, line in enumerate(lines):
            m = _DECO_RE.search(line)
            if not m:
                continue
            route_path = _extract_path(lines, idx)
            if route_path is None:
                continue
            route_path = _strip_path_prefix(route_path)
            if route_path in exempt:
                continue
            # Inspect 10 lines preceding AND 10 lines following the
            # @router.post line for an actual ``@audit_bearing(``
            # invocation. Decorator order doesn't matter to FastAPI,
            # so we accept either stacking. Restricted to the literal
            # decorator syntax so a comment containing the word
            # (e.g. "# TODO add audit_bearing") does not silently
            # satisfy the lint.
            window_before = lines[max(0, idx - 10):idx]
            window_after = lines[idx + 1:idx + 11]
            if any(_AUDIT_RE.search(w) for w in window_before):
                continue
            if any(_AUDIT_RE.search(w) for w in window_after):
                continue
            violations.append(
                f"{path.name}:{idx + 1}:{m.group('method').upper()} {route_path}"
            )
    return violations
