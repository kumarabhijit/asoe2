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

# Match a FastAPI state-mutating route decorator, capturing the path.
_ROUTE_RE = re.compile(
    r'@(?:router|app)\.(?P<method>post|put|patch|delete)\(\s*"(?P<path>[^"]+)"'
)


def _strip_path_prefix(path: str) -> str:
    """Normalise the path so EXEMPT_PATHS can match without worrying
    about the router-mount prefix.
    """
    # Routers mount under /api/v1 or similar; we strip nothing and
    # let callers decide. Paths with leading-slash are returned as-is.
    return path if path.startswith("/") else f"/{path}"


def scan_routes_for_violations(
    *,
    routes_dir: Path,
    exempt_paths: Set[str] | Iterable[str] = frozenset(),
) -> List[str]:
    """Return a list of "<file>:<line>:<method> <path>" violations.

    A violation is a state-mutating route handler whose immediately
    preceding decorator stack does NOT include ``@audit_bearing``,
    AND whose path is not in ``exempt_paths``.

    The check is intentionally source-level (a regex sweep) rather
    than runtime — it catches a new POST handler being added without
    the marker BEFORE the FastAPI app even imports.
    """
    exempt = set(exempt_paths)
    violations: List[str] = []

    for path in sorted(routes_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        lines = path.read_text().splitlines()
        for idx, line in enumerate(lines):
            m = _ROUTE_RE.search(line)
            if not m:
                continue
            route_path = _strip_path_prefix(m.group("path"))
            if route_path in exempt:
                continue
            # Inspect up to 10 lines preceding this decorator for the
            # @audit_bearing marker. FastAPI routes occasionally stack
            # multiple decorators (e.g. dependencies, response model),
            # so 10 is a comfortable upper bound.
            window = lines[max(0, idx - 10):idx]
            if any("audit_bearing" in w for w in window):
                continue
            violations.append(
                f"{path.name}:{idx + 1}:{m.group('method').upper()} {route_path}"
            )
    return violations
