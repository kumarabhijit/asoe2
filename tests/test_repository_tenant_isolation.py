"""Fitness test — every SQL path in ``db/repository.py`` filters by
``tenant_id``.

ADR-028 Guard-rail 4 / action item A7: "Tenant-isolation CI gate."

The repository layer enforces application-level tenant isolation by
including ``WHERE tenant_id = ?`` (or equivalent) on every read and
``tenant_id = ?`` on every write. PostgreSQL RLS provides
defense-in-depth, but the application-layer predicate is what tests
without a live Postgres can verify, and what reviewers can read at the
call site.

This fitness test parses ``db/repository.py`` via AST, locates every
``cursor.execute(query_string, params)`` call, and asserts that the
literal SQL query references ``tenant_id``. Two failure modes are
caught:

  1. A new method that issues a SELECT/UPDATE/DELETE without
     ``WHERE tenant_id = ?`` — the kind of regression that would
     leak rows across tenants.
  2. A new INSERT without a ``tenant_id`` column reference — the
     kind of regression that would write a tenantless row.

Allow-list: helper queries that legitimately span tenants (e.g. a
backfill or migration utility) can be added to
``_ALLOWED_TENANTLESS_QUERIES`` with a justification comment. The
allow-list is empty in V1 — every existing query references
``tenant_id``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Tuple

import pytest


_REPOSITORY_PATH = (
    Path(__file__).resolve().parent.parent / "db" / "repository.py"
)

# Queries that legitimately do not reference tenant_id. Empty in V1 —
# every existing query references tenant_id either in the WHERE clause
# or the column list (INSERT). Future legitimate exemptions (e.g. a
# read-only health probe that COUNT(*) across tenants for an admin
# dashboard) MUST add a justification comment alongside the allow-list
# entry, and ideally a CODEOWNERS path-level reviewer requirement on
# this file.
_ALLOWED_TENANTLESS_QUERIES: set[str] = set()


def _extract_sql_strings(tree: ast.AST) -> List[Tuple[str, int, str]]:
    """Walk the AST and return every literal SQL string passed to
    ``cursor.execute(...)``.

    Returns a list of ``(method_name, line_no, query_string)`` tuples
    so failures point at the offending site precisely.
    """
    results: List[Tuple[str, int, str]] = []

    # Track the enclosing method name so failures are diagnosable.
    class _ExecuteVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._method_stack: List[str] = ["<module>"]

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._method_stack.append(node.name)
            self.generic_visit(node)
            self._method_stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._method_stack.append(node.name)
            self.generic_visit(node)
            self._method_stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            # Match `<anything>.execute(<query>, ...)` — covers
            # cur.execute(...), self._cur.execute(...), etc.
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and node.args
            ):
                first_arg = node.args[0]
                literal = _coerce_to_string_literal(first_arg)
                if literal is not None:
                    results.append((
                        self._method_stack[-1],
                        node.lineno,
                        literal,
                    ))
            self.generic_visit(node)

    _ExecuteVisitor().visit(tree)
    return results


def _coerce_to_string_literal(node: ast.AST) -> str | None:
    """Return the literal string value of ``node`` when it can be
    statically resolved, else None.

    Handles plain string literals and adjacent-string concatenation
    (Python's implicit ``"foo" "bar"`` joining) which the AST
    represents as a single Constant. Triple-quoted strings, f-strings
    without interpolation, and string concatenation via ``+`` are
    also handled.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # f-string — only safe to inspect when every part is a
        # constant (no interpolation). The repository.py SQL never
        # interpolates dynamic values into the query string, so this
        # branch is here defensively.
        parts: List[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                return None
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _coerce_to_string_literal(node.left)
        right = _coerce_to_string_literal(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _normalize(sql: str) -> str:
    """Lowercase + collapse whitespace so the substring check is
    tolerant of formatting variations (line breaks in the source for
    long queries)."""
    return " ".join(sql.lower().split())


# ---------------------------------------------------------------------------
# Fitness assertions
# ---------------------------------------------------------------------------


_TREE = ast.parse(_REPOSITORY_PATH.read_text(encoding="utf-8"))
_QUERIES = _extract_sql_strings(_TREE)


def test_repository_has_some_queries():
    """Sanity check — the AST walker actually found queries.

    Without this, a future refactor that moves all queries into a
    helper module (or breaks the AST extraction) would silently make
    every other test in this file pass vacuously.
    """
    assert len(_QUERIES) >= 5, (
        f"Expected at least 5 SQL queries in db/repository.py; found "
        f"{len(_QUERIES)}. Has the AST extractor stopped working?"
    )


@pytest.mark.parametrize(
    "method,line_no,query",
    _QUERIES,
    ids=[f"{m}:{ln}" for m, ln, _ in _QUERIES],
)
def test_every_query_filters_by_tenant_id(
    method: str, line_no: int, query: str,
) -> None:
    """Every SQL string passed to cursor.execute must reference
    tenant_id, unless explicitly allow-listed.

    Failure means a regression: a SELECT/UPDATE/DELETE without
    `WHERE tenant_id = ?` would return / mutate rows across tenants;
    an INSERT without a `tenant_id` column would write a tenantless
    row that subsequent queries (which DO filter) would never find.
    """
    if query in _ALLOWED_TENANTLESS_QUERIES:
        pytest.skip(
            f"{method}:{line_no} query is in _ALLOWED_TENANTLESS_QUERIES — "
            f"verified intentional",
        )
    normalized = _normalize(query)
    assert "tenant_id" in normalized, (
        f"{method}:{line_no} — SQL query in db/repository.py does not "
        f"reference tenant_id. ADR-028 Guard-rail 4 / action item A7 "
        f"requires every SQL path to filter by tenant_id (application-"
        f"level tenant isolation). Either:\n"
        f"  1. Add `WHERE tenant_id = ?` (SELECT/UPDATE/DELETE) or a "
        f"     `tenant_id` column reference (INSERT), OR\n"
        f"  2. Add this query to _ALLOWED_TENANTLESS_QUERIES with a "
        f"     justification comment (cross-tenant utility, etc.) and "
        f"     get compliance sign-off.\n"
        f"\nQuery:\n{query}"
    )


def test_no_select_without_where_tenant_id():
    """Stronger check: SELECT statements specifically must include a
    WHERE clause (not just mention tenant_id incidentally in a column
    list)."""
    offenders: List[str] = []
    for method, line_no, query in _QUERIES:
        if query in _ALLOWED_TENANTLESS_QUERIES:
            continue
        normalized = _normalize(query)
        if not normalized.startswith("select"):
            continue
        if "where" not in normalized:
            offenders.append(f"{method}:{line_no}")
            continue
        # Crude but effective: tenant_id must appear in the part of the
        # query AFTER the first WHERE keyword.
        where_part = normalized.split("where", 1)[1]
        if "tenant_id" not in where_part:
            offenders.append(f"{method}:{line_no}")
    assert not offenders, (
        f"SELECT queries missing tenant_id in WHERE clause: {offenders}. "
        f"See db/repository.py at the listed line numbers."
    )


def test_no_update_without_where_tenant_id():
    """UPDATE statements must filter by tenant_id in their WHERE clause —
    otherwise they could mutate rows across tenants."""
    offenders: List[str] = []
    for method, line_no, query in _QUERIES:
        if query in _ALLOWED_TENANTLESS_QUERIES:
            continue
        normalized = _normalize(query)
        if not normalized.startswith("update"):
            continue
        if "where" not in normalized:
            offenders.append(f"{method}:{line_no} (no WHERE clause)")
            continue
        where_part = normalized.split("where", 1)[1]
        if "tenant_id" not in where_part:
            offenders.append(f"{method}:{line_no}")
    assert not offenders, (
        f"UPDATE queries missing tenant_id in WHERE clause: {offenders}."
    )


def test_no_delete_without_where_tenant_id():
    """DELETE statements must filter by tenant_id. Currently
    repository.py has no DELETEs (V003 migration rejects DELETE on
    policy_audit_log; exception writes are immutable). This test
    catches a future regression."""
    offenders: List[str] = []
    for method, line_no, query in _QUERIES:
        if query in _ALLOWED_TENANTLESS_QUERIES:
            continue
        normalized = _normalize(query)
        if not normalized.startswith("delete"):
            continue
        if "where" not in normalized:
            offenders.append(f"{method}:{line_no} (no WHERE clause)")
            continue
        where_part = normalized.split("where", 1)[1]
        if "tenant_id" not in where_part:
            offenders.append(f"{method}:{line_no}")
    assert not offenders, (
        f"DELETE queries missing tenant_id in WHERE clause: {offenders}."
    )


def test_repository_methods_accept_tenant_id_parameter():
    """Every public method on the *Repository classes whose body issues
    a query MUST accept a tenant_id parameter. Catches the case where
    a method gets defined that doesn't take tenant_id and so could
    never pass the WHERE filter even if the query were correct."""
    offenders: List[str] = []
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.ClassDef):
            continue
        if not node.name.endswith("Repository"):
            continue
        for child in node.body:
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            method_name = child.name
            if method_name.startswith("_"):
                continue  # private helpers may take a cur+tenant_id pair
            # Public method — must take tenant_id (positional or kwarg).
            arg_names = [a.arg for a in child.args.args] + [
                a.arg for a in child.args.kwonlyargs
            ]
            # Skip methods that don't actually issue a query (e.g. a
            # __init__ or a pure helper). Detect via a presence check
            # on cursor.execute in the method body.
            issues_query = False
            for inner in ast.walk(child):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "execute"
                ):
                    issues_query = True
                    break
            if not issues_query:
                continue
            if "tenant_id" not in arg_names:
                offenders.append(f"{node.name}.{method_name}")
    assert not offenders, (
        f"Repository methods that issue queries without a tenant_id "
        f"parameter: {offenders}. Every query path must take tenant_id "
        f"so the WHERE clause has the value to bind."
    )
